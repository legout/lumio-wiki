#!/usr/bin/env bash
# Scriptable smoke journey for the canonical onboarding quickstart (issue #180).
#
# Executes EXACTLY the commands documented in docs/quickstart.md, in order:
# local Maintainer setup -> ontology starter -> seed pages -> managed ingest ->
# proposal review -> publish -> retrieval/citation/traversal -> Source Artifact
# inspection -> (when MinIO is configured) S3 publication with and without
# LanceDB -> read-only Reader project -> expected zero-index fallback.
#
# Any command failing or printing an unexpected outcome fails the script, so
# documentation drift between the quickstart and the CLI is caught here.
#
# Usage:
#   ./smoke-journey.sh [project-dir]        # default: a fresh temp dir
#
# Environment:
#   LUMIO_WIKI_BIN   lumio-wiki invocation (default: "lumio-wiki";
#                     e.g. "uv --project /path/to/lumio run lumio-wiki")
#   LUMIO_PYTHON     python with obstore for bucket creation fallback
#                     (default: python3)
#
# MinIO part runs only when ALL of these are set (same variables the CLI and
# the MinIO test suites read):
#   LUMIO_S3_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
# Optional: LUMIO_S3_REGION (default us-east-1), LUMIO_S3_TEST_BUCKET
#           (default lumio-quickstart), MC_BIN (default mc).

set -euo pipefail

LW=${LUMIO_WIKI_BIN:-lumio-wiki}
BUCKET=${LUMIO_S3_TEST_BUCKET:-lumio-quickstart}
# Per-run prefix keeps repeated smoke journeys from colliding with immutable
# published versions (they cannot be overwritten).
PREFIX="helpdesk-kb/run-$(date +%Y%m%d%H%M%S)-$$"
HERE=$(cd "$(dirname "$0")" && pwd)
PROJ=${1:-$(mktemp -d)}
S3_READY=0
if [ -n "${LUMIO_S3_ENDPOINT:-}" ] && [ -n "${AWS_ACCESS_KEY_ID:-}" ] \
  && [ -n "${AWS_SECRET_ACCESS_KEY:-}" ]; then
  S3_READY=1
fi

step() { printf '\n== %s\n' "$*"; }
expect_fail() { # expect_fail <label> <cmd...> : command must exit non-zero
  local label=$1; shift
  if "$@" >/dev/null 2>&1; then
    echo "FAIL: expected a non-zero exit (truthful unavailability): $label" >&2
    exit 1
  fi
}

step "0. Project: $PROJ"
mkdir -p "$PROJ"; cd "$PROJ"

step "1. Maintainer setup"
if [ "$S3_READY" = 1 ]; then
  $LW setup ./kb --publish-to "s3://$BUCKET/$PREFIX"
else
  echo "(no MinIO endpoint configured — plain local setup, S3 steps skipped)"
  $LW setup ./kb
fi

step "2. Ontology starter (replace the empty ontology block in kb/lumio.yaml)"
${LUMIO_PYTHON:-python3} - <<'PYEOF'
from pathlib import Path

path = Path("kb/lumio.yaml")
text = path.read_text(encoding="utf-8")
old = """ontology:
  entity_types:
  predicates:
  redirects:
"""
new = """ontology:
  entity_types:
    software-system:
      description: "A deployed software product or platform."
    database:
      description: "A persistent storage service backing an application."
    procedure:
      description: "A reviewed runbook or operating procedure."
  predicates:
    uses:
      subject_types: [software-system]
      object_types: [database, software-system]
      inverse: used-by
    used-by:
      subject_types: [database, software-system]
      object_types: [software-system]
      inverse: uses
    described-as:
      literal_kind: string
  redirects: {}
"""
if old not in text:
    raise SystemExit("FAIL: seeded empty ontology block not found in kb/lumio.yaml")
path.write_text(text.replace(old, new), encoding="utf-8")
PYEOF

step "3. Seed pages (one accepted Claim edge: Aurora uses Starlight DB)"
mkdir -p kb/entities
cat > kb/entities/aurora-helpdesk.md <<'PAGEEOF'
---
title: "Aurora Helpdesk"
id: "entity:aurora-helpdesk"
entity_types: ["software-system"]
aliases: ["Aurora"]
tags: ["support", "product"]
summary: "The Aurora helpdesk product this Knowledge Base documents."
lifecycle: "approved"
visibility: "internal"
sources:
  - id: "aurora-product-notes"
    title: "Aurora product notes"
claims:
  - id: "claim:aurora-uses-starlight"
    predicate: "uses"
    object: "entity:starlight-db"
    status: "accepted"
    evidence:
      - lines: [6, 6]
---

# Aurora Helpdesk

Aurora is the support helpdesk this Knowledge Base documents. It stores
tickets and attachments in [Starlight DB](starlight-db.md).
PAGEEOF
cat > kb/entities/starlight-db.md <<'PAGEEOF'
---
title: "Starlight DB"
id: "entity:starlight-db"
entity_types: ["database"]
tags: ["storage"]
summary: "The persistent database behind Aurora Helpdesk."
lifecycle: "approved"
visibility: "internal"
sources:
  - id: "aurora-product-notes"
    title: "Aurora product notes"
claims:
  - id: "claim:starlight-described-as"
    predicate: "described-as"
    value: "managed PostgreSQL, region eu-west-1"
    value_type: "string"
    status: "accepted"
    evidence:
      - lines: [3, 4]
---

# Starlight DB

Starlight DB is the managed PostgreSQL (region eu-west-1) that stores Aurora
tickets and attachments.
PAGEEOF
$LW validate ./kb

step "4. Managed document ingest (original source + authored page, one identity)"
cat > password-reset-page.md <<'PAGEEOF'
---
title: "Password Reset Runbook"
category: procedures
type: runbook
durability_rationale: "Owned runbook; reviewed yearly by Support Ops."
id: "entity:password-reset-runbook"
entity_types: ["procedure"]
tags: ["support", "runbook"]
summary: "How Aurora support verifies a requester and forces a password reset."
lifecycle: "approved"
visibility: "internal"
sources:
  - id: "support-runbook-2026"
    title: "Support runbook: password reset"
---

# Password Reset Runbook

Verified steps from the 2026 support runbook. Aurora Helpdesk support
verifies the requester through the secondary email on file, then forces the
reset from the requester's profile. The reset link expires after 30 minutes.
Requests without a secondary email escalate to Support Ops on-call; reset
volume is tracked in [Starlight DB](../entities/starlight-db.md).
PAGEEOF
$LW ingest ./kb "$HERE/sources/support-runbook.md" \
  --compiled-page password-reset-page.md --source-id support-runbook-2026

step "5. Proposal review: list -> inspect -> validate -> publish"
PID=$($LW proposal list ./kb | awk '/staged/{print $1}')
$LW proposal inspect ./kb "$PID" | tee /dev/stderr | grep -Eq 'affected_pages: +Password Reset Runbook'
$LW proposal validate ./kb "$PID"
$LW publish ./kb "$PID"

step "6. Retrieval, citation open actions, graph traversal"
$LW search "password reset" --limit 2 | tee /dev/stderr | grep -q '^open:'
$LW page "Password Reset Runbook" | tee /dev/stderr | grep -q '^source-artifact:'
$LW related "Aurora Helpdesk" --scope canonical --trace | tee /dev/stderr | grep -q 'Starlight DB'
$LW paths "Aurora Helpdesk" "Starlight DB" --scope canonical --trace \
  | tee /dev/stderr | grep -q 'found=true'

step "7. Source Artifact inspection: truthful unavailability (no store configured)"
$LW source inspect ./kb --source-id support-runbook-2026 \
  | tee /dev/stderr | grep -q 'not retained'
expect_fail "local source fetch without a Source Artifact Store" \
  $LW source fetch ./kb --source-id support-runbook-2026 --output /tmp/should-not-exist.md

if [ "$S3_READY" != 1 ]; then
  echo
  echo "PASS: local onboarding journey complete (S3/MinIO steps skipped —"
  echo "set LUMIO_S3_ENDPOINT + AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY to run them)."
  exit 0
fi

step "8. Publish immutable S3 version (zero-index) to s3://$BUCKET/$PREFIX"
export LUMIO_S3_ALLOW_HTTP=${LUMIO_S3_ALLOW_HTTP:-}
case "$LUMIO_S3_ENDPOINT" in
  http://*) export LUMIO_S3_ALLOW_HTTP=1 ;;
esac
# Bucket creation is a one-time operator step documented in the quickstart
# (mc mb / aws s3api create-bucket). With no mc on PATH, verify the endpoint
# is reachable through the same client the CLI uses; a missing bucket then
# fails loudly at publish-s3 below with an actionable error.
if command -v "${MC_BIN:-mc}" >/dev/null 2>&1; then
  "${MC_BIN:-mc}" alias set local "$LUMIO_S3_ENDPOINT" \
    "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" >/dev/null
  "${MC_BIN:-mc}" mb --ignore-existing "local/$BUCKET" >/dev/null
else
  ${LUMIO_PYTHON:-python3} - "$BUCKET" <<'PYEOF'
import os
import sys

import obstore

endpoint = os.environ["LUMIO_S3_ENDPOINT"]
config = {
    "aws_region": os.environ.get("LUMIO_S3_REGION", "us-east-1"),
    "aws_endpoint": endpoint,
    "aws_access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
    "aws_secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
}
client = {"allow_http": True} if endpoint.startswith("http://") else {}
store = obstore.store.from_url(f"s3://{sys.argv[1]}", config=config, client_options=client)
try:
    list(obstore.list(store, prefix="journey-reachability-check/"))
except Exception as exc:
    sys.exit(f"FAIL: cannot use bucket s3://{sys.argv[1]} at {endpoint}: {exc}; "
             "create it with 'mc mb local/<bucket>' (see docs/quickstart.md Part 1)")
PYEOF
fi
$LW publish-s3 --version v1

step "9. Reader: read-only project against the S3 Location"
READER=$(mktemp -d)
( cd "$READER"
  $LW setup --from "s3://$BUCKET/$PREFIX"
  $LW search "password reset" --limit 2 | tee /dev/stderr | grep -q '^open:'
  $LW related "Aurora Helpdesk" --scope canonical | tee /dev/stderr | grep -q 'Starlight DB'
  expect_fail "reader source inspect without a Source Artifact Store" \
    $LW source inspect --source-id support-runbook-2026
)

step "10. Expected zero-index fallback (lancedb requested, index not published yet)"
READER_LANCE=$(mktemp -d)
( cd "$READER_LANCE"
  $LW setup --from "s3://$BUCKET/$PREFIX" --retrieval lancedb \
    | tee /dev/stderr | grep -q 'zero-index page search over the same Published Version'
  $LW search "password reset" --limit 1 | tee /dev/stderr | grep -q '^open:'
)

step "11. Publish v2 WITH remote LanceDB (CAS guard), reader becomes healthy"
$LW publish-s3 --version v2 --expected-pointer-version v1 --retrieval lancedb
( cd "$READER_LANCE"
  $LW setup --from "s3://$BUCKET/$PREFIX" --retrieval lancedb \
    | tee /dev/stderr | grep -Eq 'lancedb_healthy: +true'
  # Retrieval MODE stays separate from the backend: lexical works everywhere;
  # semantic/hybrid truthfully demand an embedder (none configured here).
  $LW search "password reset" --mode lexical --limit 1 | tee /dev/stderr | grep -q '^open:'
  expect_fail "hybrid mode without an embedder" \
    $LW search "password reset" --mode hybrid
)

echo
echo "PASS: full onboarding journey complete against $LUMIO_S3_ENDPOINT (bucket $BUCKET)."
