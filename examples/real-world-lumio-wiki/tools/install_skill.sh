#!/usr/bin/env bash
# Install the packaged lumio-wiki Agent Skill into a coding agent's local
# skill directory. Idempotent: re-running reports the destination and exits 0.
#
# Usage: tools/install_skill.sh [--agent pi|hermes|codex|claude-code]
#
# After installation, the target agent must be restarted (or a new session
# must be started) so its skill discovery picks the new SKILL.md up.
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$EXAMPLE_DIR/.venv"
LUMIO="$VENV/bin/lumio-wiki"

if [[ ! -x "$LUMIO" ]]; then
	echo "error: $LUMIO not found; run ./bootstrap.sh first" >&2
	exit 1
fi

DEFAULT_AGENT="${LUMIO_SKILL_AGENT:-pi}"
AGENT="${1:-}"
if [[ -z "$AGENT" ]]; then
	if [[ "$DEFAULT_AGENT" == --* ]]; then
		AGENT="pi"
	else
		AGENT="$DEFAULT_AGENT"
	fi
	case "$DEFAULT_AGENT" in
	--agent)
		AGENT="${2:-pi}"
		;;
	esac
fi

if [[ "$AGENT" == --agent=* ]]; then
	AGENT="${AGENT#--agent=}"
fi

printf '\n=== skill status ===\n'
"$LUMIO" skill status || true

printf '\n=== skill install --agent %s ===\n' "$AGENT"
"$LUMIO" skill install --agent "$AGENT" --overwrite

printf '\nInstalled. Restart %s (or start a new session) so skill discovery picks SKILL.md up.\n' "$AGENT"
