#!/usr/bin/env bash
# Run the read-only maintenance pass on the example Knowledge Base.
#
# Each step prints its label and the command before running it, so a review
# transcript records what was inspected. Steps that exit non-zero abort the
# run with a clear error code. The script never mutates the KB.
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$EXAMPLE_DIR/.venv"
LUMIO="$VENV/bin/lumio-wiki"
KB="${LUMIO_KB_PATH:-$EXAMPLE_DIR/knowledge-base}"

if [[ ! -x "$LUMIO" ]]; then
	echo "error: $LUMIO not found; run ./bootstrap.sh first" >&2
	exit 1
fi

if [[ ! -d "$KB" ]]; then
	echo "error: Knowledge Base not found at $KB; run 'lumio-wiki setup $KB' first" >&2
	exit 1
fi

run_step() {
	local label="$1"
	shift
	printf '\n=== %s ===\n' "$label"
	printf '$ %s\n' "$*"
	"$@"
}

export LUMIO_KB_PATH="$KB"

run_step "validate" "$LUMIO" validate "$KB"
run_step "health" "$LUMIO" health "$KB"
run_step "lint" "$LUMIO" lint "$KB"
run_step "cross-link --limit 5 (read-only)" "$LUMIO" cross-link --limit 5 "$KB"
run_step "dream --limit 5 (read-only)" "$LUMIO" dream --limit 5 "$KB"
run_step "source list" "$LUMIO" source list "$KB"

printf '\nMaintenance pass complete (no proposals staged).\n'
