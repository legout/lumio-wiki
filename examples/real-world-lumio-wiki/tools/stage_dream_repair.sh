#!/usr/bin/env bash
# Stage up to N reviewable repair proposals from the Dream Cycle.
#
# This is the script the coding agent can run after `run_maintenance.sh` to
# convert the dream's ranked candidates into actual proposals. The agent is
# expected to then walk proposal inspect → proposal validate → publish for
# each staged id before any change reaches the Knowledge Base.
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$EXAMPLE_DIR/.venv"
LUMIO="$VENV/bin/lumio-wiki"
KB="${LUMIO_KB_PATH:-$EXAMPLE_DIR/knowledge-base}"
LIMIT="${DREAM_LIMIT:-3}"

if [[ ! -x "$LUMIO" ]]; then
	echo "error: $LUMIO not found; run ./bootstrap.sh first" >&2
	exit 1
fi

if [[ ! -d "$KB" ]]; then
	echo "error: Knowledge Base not found at $KB; run 'lumio-wiki setup $KB' first" >&2
	exit 1
fi

export LUMIO_KB_PATH="$KB"

printf '\n=== dream --stage --limit %s ===\n' "$LIMIT"
printf '$ %s dream --stage --limit %s %s\n' "$LUMIO" "$LIMIT" "$KB"
"$LUMIO" dream --stage --limit "$LIMIT" "$KB"

printf '\n=== proposal list ===\n'
printf '$ %s proposal list %s\n' "$LUMIO" "$KB"
"$LUMIO" proposal list "$KB"

printf '\nFor each staged proposal run, in order:\n'
printf '  %s proposal inspect <id> %s\n' "$LUMIO" "$KB"
printf '  %s proposal validate <id> %s\n' "$LUMIO" "$KB"
printf '  %s publish <id> %s    # or: %s discard <id> %s\n' "$LUMIO" "$KB" "$LUMIO" "$KB"
