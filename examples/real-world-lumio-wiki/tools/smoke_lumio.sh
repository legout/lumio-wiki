#!/usr/bin/env bash
# "Finally" stage browser/API smoke (issue #157): boot the full `lumio`
# application over the published Atlas Heatworks Knowledge Base and prove the
# same content is consumable through Lumio's HTTP surface — without breaking the
# proposal-first publication contract (this script only READS; it publishes
# nothing).
#
# What it does, end to end, fully offline:
#   1. validates the published Knowledge Base with `lumio-wiki validate`;
#   2. starts `lumio serve` on a free local port (PID -> .lumio.pid) over a
#      fresh, throwaway data directory so first-run `/setup` works every run;
#   3. polls GET /health until the app is ready;
#   4. runs the first-run Owner setup, logs in, and asks ONE question through
#      the OpenAI-compatible POST /v1/chat/completions endpoint (the scriptable
#      equivalent of the issue's "/api/chat POST" — see README product
#      findings). No provider is configured, so the app uses the offline
#      FakeProvider and never makes a network call;
#   5. asserts the answer is covered and carries at least one citation;
#   6. writes the full transcript to lumio-smoke/<timestamp>/transcript.json;
#   7. tears the server down and removes the PID file.
#
# Prerequisites (from this directory):
#   ./bootstrap.sh           # base install: lumio-wiki[documents]
#   ./bootstrap-lumio.sh     # layer the full lumio app onto the same venv
#   lumio-wiki setup ./knowledge-base + ingest + publish (see README walkthrough)
#
# Hermetic: no live network calls and no telemetry beyond what `lumio` itself
# emits. Do NOT set LUMIO_PROVIDER_* — the unset provider keeps the app on the
# offline FakeProvider.
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$EXAMPLE_DIR/.venv"
LUMIO="$VENV/bin/lumio"
LUMIO_WIKI="$VENV/bin/lumio-wiki"

# The published Knowledge Base to serve. Defaults to the example's KB; the
# working directory the app writes to is a throwaway copy (shared storage pulls
# the source into an isolated working dir), so the published KB is never
# mutated by this smoke.
KB_SOURCE="${LUMIO_KB_PATH:-$EXAMPLE_DIR/knowledge-base}"
SMOKE_ROOT="${LUMIO_SMOKE_DIR:-$EXAMPLE_DIR/lumio-smoke}"
HOST="${LUMIO_SMOKE_HOST:-127.0.0.1}"
OWNER_PASSWORD="${LUMIO_SMOKE_OWNER_PASSWORD:-smoke-owner-pass-123}"
# The question is drawn from evaluation/questions.md (Factual retrieval #1).
# Override with LUMIO_SMOKE_QUESTION to repoint the smoke at a different page.
QUESTION="${LUMIO_SMOKE_QUESTION:-What response target applies when a customer loses all heating below 5°C?}"
HEALTH_TIMEOUT="${LUMIO_SMOKE_HEALTH_TIMEOUT:-30}"   # seconds to wait for /health
PID_FILE="$EXAMPLE_DIR/.lumio.pid"

log() { printf '[smoke] %s\n' "$*"; }
die() { printf '[smoke] error: %s\n' "$*" >&2; exit 1; }

# --- prerequisites -----------------------------------------------------------
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v jq >/dev/null 2>&1 || die "jq is required"
[[ -x "$LUMIO" ]] || die "lumio not found at $LUMIO; run ./bootstrap-lumio.sh first"
[[ -x "$LUMIO_WIKI" ]] || die "lumio-wiki not found at $LUMIO_WIKI; run ./bootstrap.sh first"
[[ -d "$KB_SOURCE" ]] || die "Knowledge Base not found at $KB_SOURCE; publish it first (see README walkthrough)"

# Confirm the KB is valid before booting the app over it.
log "validating Knowledge Base at $KB_SOURCE ..."
"$LUMIO_WIKI" validate "$KB_SOURCE" >/dev/null

# --- isolated run directory --------------------------------------------------
TS="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR="$SMOKE_ROOT/$TS"
DATA_DIR="$RUN_DIR/data"      # fresh LUMIO_CONFIG_PATH -> fresh auth/audit DB
WORKING_DIR="$RUN_DIR/kb"     # app working copy (shared storage pulls source here)
COOKIE_JAR="$RUN_DIR/cookies.txt"
SERVER_LOG="$RUN_DIR/server.log"
TRANSCRIPT="$RUN_DIR/transcript.json"
mkdir -p "$DATA_DIR" "$WORKING_DIR"

# --- find a free local port --------------------------------------------------
PORT="$("$VENV/bin/python" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
BASE_URL="http://$HOST:$PORT"
log "run dir: $RUN_DIR"
log "base url: $BASE_URL (free port $PORT)"

# --- start lumio serve in the background -------------------------------------
# Hermetic guardrail (#157): scrub any caller-provided provider config so the
# app boots on the offline FakeProvider and never makes a network call. Do NOT
# set these in the caller environment if you want a real provider.
unset LUMIO_PROVIDER_BASE_URL LUMIO_PROVIDER_MODEL LUMIO_PROVIDER_API_KEY 2>/dev/null || true

# Shared storage serves the published KB from a local directory; the app pulls
# it into WORKING_DIR and serves retrieval/answers from there. A fresh
# LUMIO_CONFIG_PATH AND a fresh LUMIO_METADATA_DB_PATH give every run a clean
# first-run state so /setup succeeds (the auth/audit/chat tables live in one
# metadata SQLite DB controlled by LUMIO_METADATA_DB_PATH — defaulting to
# ./lumio.sqlite at the CWD — so it MUST be isolated per run or the Owner from a
# previous run makes /setup return 409).
LUMIO_KB_PATH="$WORKING_DIR" \
LUMIO_STORAGE_MODE=shared \
LUMIO_SHARED_SOURCE="$KB_SOURCE" \
LUMIO_CONFIG_PATH="$DATA_DIR" \
LUMIO_METADATA_DB_PATH="$DATA_DIR/lumio.sqlite" \
STARIO_HOST="$HOST" \
STARIO_PORT="$PORT" \
"$LUMIO" serve --host "$HOST" --port "$PORT" >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" >"$PID_FILE"
log "started lumio serve (pid $SERVER_PID); log: $SERVER_LOG"

cleanup() {
	local code=$?
	if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
		log "stopping lumio serve (pid $SERVER_PID) ..."
		kill "$SERVER_PID" 2>/dev/null || true
		wait "$SERVER_PID" 2>/dev/null || true
	fi
	rm -f "$PID_FILE"
	exit "$code"
}
trap cleanup EXIT INT TERM

# --- helper: JSON POST with status-code check --------------------------------
# Usage: json_post <path> <json-body> [cookie-jar]
# Prints the response body; fails (return != 0) on a non-2xx status.
json_post() {
	local path="$1" body="$2" jar="${3:-}"
	local code tmp
	tmp="$(mktemp)"
	local curl_args=(-sS -X POST -H 'content-type: application/json' -d "$body" -o "$tmp" -w '%{http_code}')
	[[ -n "$jar" ]] && curl_args+=(-b "$jar" -c "$jar")
	code="$(curl "${curl_args[@]}" "$BASE_URL$path")" || { cat "$tmp" >&2 || true; rm -f "$tmp"; die "request to $path failed"; }
	if [[ "$code" -lt 200 || "$code" -ge 300 ]]; then
		cat "$tmp" >&2 || true
		rm -f "$tmp"
		die "POST $path returned HTTP $code (see $SERVER_LOG)"
	fi
	cat "$tmp"
	rm -f "$tmp"
}

# --- poll /health until ready ------------------------------------------------
log "waiting for /health (up to ${HEALTH_TIMEOUT}s) ..."
ready=0
for _ in $(seq 1 "$HEALTH_TIMEOUT"); do
	if curl -sS -o /dev/null "$BASE_URL/health" 2>/dev/null; then
		ready=1
		break
	fi
	sleep 1
done
[[ "$ready" -eq 1 ]] || die "app did not become healthy within ${HEALTH_TIMEOUT}s (see $SERVER_LOG)"
HEALTH_BODY="$(curl -sS "$BASE_URL/health")"
log "healthy: $HEALTH_BODY"

# --- first-run Owner setup + login -------------------------------------------
# Fresh LUMIO_CONFIG_PATH => no users exist, so /setup creates the Owner.
log "first-run Owner setup ..."
json_post /setup "{\"username\":\"owner\",\"password\":\"$OWNER_PASSWORD\",\"role\":\"owner\"}" >/dev/null
log "logging in as Owner ..."
json_post /login "{\"username\":\"owner\",\"password\":\"$OWNER_PASSWORD\"}" "$COOKIE_JAR" >/dev/null

# --- ask ONE cited question --------------------------------------------------
# /v1/chat/completions is the OpenAI-compatible JSON endpoint behind the same
# Chat Gateway as the /chat UI. An Owner (Role 3) satisfies require_reader
# (Role 1), so no separate Reader account is needed for the smoke.
log "asking: $QUESTION"
ANSWER="$(json_post /v1/chat/completions \
	"{\"model\":\"lumio\",\"messages\":[{\"role\":\"user\",\"content\":$(jq -Rs . <<<"$QUESTION")}]}" \
	"$COOKIE_JAR")"

# --- assert covered + citation ----------------------------------------------
COVERED="$(jq -r '.lumio.covered' <<<"$ANSWER")"
CITATION_COUNT="$(jq '.lumio.citations | length' <<<"$ANSWER")"
log "covered=$COVERED citations=$CITATION_COUNT"
CITATION_TITLES="$(jq -r '.lumio.citations | map(.page_title) | join(", ")' <<<"$ANSWER")"
[[ -n "$CITATION_TITLES" ]] && log "cited pages: $CITATION_TITLES"

if [[ "$COVERED" != "true" || "$CITATION_COUNT" -lt 1 ]]; then
	# Persist the failing transcript before bailing so it can be inspected.
	jq -n \
		--arg ts "$TS" \
		--arg question "$QUESTION" \
		--arg base_url "$BASE_URL" \
		--arg covered "$COVERED" \
		--argjson citation_count "$CITATION_COUNT" \
		--argjson answer "$ANSWER" \
		--arg health "$HEALTH_BODY" \
		--arg server_log "$SERVER_LOG" \
		'{timestamp:$ts, base_url:$base_url, health:$health, question:$question,
		  verdict:"FAIL", covered:($covered=="true"), citation_count:$citation_count,
		  answer:$answer, server_log:$server_log}' >"$TRANSCRIPT"
	die "answer was not covered or carried no citation (verdict FAIL); transcript: $TRANSCRIPT; server log: $SERVER_LOG"
fi

# --- write the transcript ----------------------------------------------------
jq -n \
	--arg ts "$TS" \
	--arg question "$QUESTION" \
	--arg base_url "$BASE_URL" \
	--argjson citation_count "$CITATION_COUNT" \
	--arg cited "$CITATION_TITLES" \
	--argjson answer "$ANSWER" \
	--arg health "$HEALTH_BODY" \
	--arg server_log "$SERVER_LOG" \
	'{timestamp:$ts, base_url:$base_url, health:$health, question:$question,
	  verdict:"PASS", covered:true, citation_count:$citation_count,
	  cited_pages:$cited, answer:$answer, server_log:$server_log}' >"$TRANSCRIPT"
log "transcript: $TRANSCRIPT"

log "PASS: covered answer with $CITATION_COUNT citation(s) over $KB_SOURCE"
