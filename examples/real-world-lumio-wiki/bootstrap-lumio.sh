#!/usr/bin/env bash
# "Finally" stage bootstrap (issue #157): add the full `lumio` application to
# the SAME isolated venv that ./bootstrap.sh created, then print the install
# state. The "Finally" stage exercises the same published Knowledge Base
# through Lumio's browser UI / OpenAI-compatible API instead of the
# coding-agent CLI.
#
# ./bootstrap.sh installs lumio-wiki[documents] only (the "Now" stage). This
# script layers the deployable `lumio` app on top without recreating the venv,
# so the base install is preserved. It prints `lumio-wiki doctor` and
# `lumio --version`, then `tools/smoke_lumio.sh` boots `lumio serve` over the
# published Atlas Heatworks Knowledge Base.
#
# `lumio` (packages/lumio) is a SEPARATE package from both lumio-wiki and
# lumio-lancedb. It HARD-DEPENDS on lumio-lancedb (>=0.1.1) plus lancedb and
# pyarrow (see packages/lumio/pyproject.toml): the deployable app ships with
# LanceDB retrieval wired in and degrades to zero-index when no published
# LanceDB index is present. That hard dependency is why this script does NOT
# guard on lumio-lancedb being present (installing `lumio` always pulls it) —
# see "Finally" > "Product findings" in README.md for the deviation from the
# issue's literal wording. Instead it guards on the meaningful, symmetric
# stage-separation condition: refuse if `lumio` is already installed.
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$EXAMPLE_DIR/../.." && pwd)"
VENV="$EXAMPLE_DIR/.venv"
PACKAGE_LUMIO="$REPO_ROOT/packages/lumio"

command -v uv >/dev/null 2>&1 || {
	echo "error: uv is required (https://docs.astral.sh/uv/)" >&2
	exit 1
}

if [[ ! -x "$VENV/bin/python" ]]; then
	echo "error: $VENV not found or incomplete (no bin/python); run ./bootstrap.sh first" >&2
	exit 1
fi

# Confirm lumio-wiki is present before layering the app on top. The "Finally"
# stage layers onto the SAME venv that ./bootstrap.sh created.
"$VENV/bin/python" - <<'PY'
from importlib.metadata import PackageNotFoundError, version

try:
	print(f"lumio-wiki {version('lumio-wiki')}")
except PackageNotFoundError:
	raise SystemExit(
		"error: lumio-wiki is not installed in this venv. Run ./bootstrap.sh first."
	)
PY

# Refuse if the full `lumio` application is already installed in this venv. The
# "Finally" stage layers `lumio` onto a clean lumio-wiki venv; an existing
# `lumio` install means the stage was already bootstrapped (or the venv was
# reused from another stage). Recreate the venv with ./bootstrap.sh for a clean
# "Finally" stage. (This is the symmetric mirror of bootstrap-lancedb.sh's
# "refuse if lumio installed" guard. The issue's literal "refuse if
# lumio-lancedb installed without the lancedb extra" is necessarily void
# because `lumio` hard-depends lumio-lancedb — see README product findings.)
"$VENV/bin/python" - <<'PY'
from importlib.metadata import PackageNotFoundError, version

try:
	installed = version("lumio")
except PackageNotFoundError:
	print("lumio: not installed (ok)")
else:
	raise SystemExit(
		f"error: the full `lumio` application ({installed}) is already installed in this venv. "
		"The Finally stage layers lumio onto a clean lumio-wiki venv; recreate the venv with "
		"./bootstrap.sh before bootstrapping lumio."
	)
PY

# Install the deployable `lumio` application into the existing venv (no
# --clear): the base lumio-wiki[documents] install is preserved, only the app
# and its operational stack (stario, piccolo, lancedb, …) are added.
echo "Installing $PACKAGE_LUMIO into the existing venv (base install preserved)..."
uv pip install --python "$VENV/bin/python" "$PACKAGE_LUMIO"

# Verify the app entry point and the optional-retrieval stack landed.
"$VENV/bin/python" - <<'PY'
from importlib.metadata import PackageNotFoundError, version

try:
	print(f"lumio {version('lumio')}")
except PackageNotFoundError:
	raise SystemExit("error: lumio did not install")

# The deployable app hard-depends on lumio-lancedb, lancedb, and pyarrow. Probe
# them with find_spec (never an import) so we report availability without
# pulling the modules into this process.
import importlib.util

missing = [
	name for name in ("lumio_lancedb", "lancedb", "pyarrow", "stario")
	if importlib.util.find_spec(name) is None
]
if missing:
	raise SystemExit(
		f"error: lumio installed but modules missing: {', '.join(missing)}"
	)
print("lumio app: importable (lumio_lancedb, lancedb, pyarrow, stario present)")
PY

# `lumio --version` confirms the console-script entry point resolved.
"$VENV/bin/lumio" --version
# `lumio-wiki doctor` re-prints the base install + extras state (now with the
# lancedb adapter visible because lumio pulled it in).
"$VENV/bin/lumio-wiki" doctor

printf '\nFinally stage ready. lumio layered onto the base install.\n'
printf 'Publish the Knowledge Base (see README end-to-end walkthrough), then:\n'
printf '  bash tools/smoke_lumio.sh\n'
