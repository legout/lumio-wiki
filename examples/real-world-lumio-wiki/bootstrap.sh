#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$EXAMPLE_DIR/../.." && pwd)"
VENV="$EXAMPLE_DIR/.venv"
PACKAGE="$REPO_ROOT/packages/lumio-wiki[documents]"

command -v uv >/dev/null 2>&1 || {
	echo "error: uv is required (https://docs.astral.sh/uv/)" >&2
	exit 1
}

uv venv --python 3.14 --clear "$VENV"
uv pip install --python "$VENV/bin/python" "$PACKAGE"

"$VENV/bin/python" - <<'PY'
from importlib.metadata import PackageNotFoundError, version

print(f"lumio-wiki {version('lumio-wiki')}")
for forbidden in ("lumio-lancedb", "lumio"):
    try:
        installed = version(forbidden)
    except PackageNotFoundError:
        print(f"{forbidden}: not installed")
    else:
        raise SystemExit(f"error: unexpected distribution installed: {forbidden} {installed}")
PY

"$VENV/bin/lumio-wiki" doctor

printf '\nReady. Start a coding agent in:\n  %s\n' "$EXAMPLE_DIR"
printf 'Activate the environment with:\n  source .venv/bin/activate\n'
