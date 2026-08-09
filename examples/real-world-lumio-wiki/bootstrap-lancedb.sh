#!/usr/bin/env bash
# "Later" stage bootstrap (issue #156): add lumio-lancedb to the SAME isolated
# venv that ./bootstrap.sh created, then print the extras state.
#
# ./bootstrap.sh installs lumio-wiki[documents] and refuses lumio-lancedb (the
# base install must stay model- and LanceDB-free). This script layers the
# optional lumio-lancedb adapter on top without recreating the venv, so the base
# install is untouched and `lumio-wiki doctor` flips extra[lancedb] to
# "installed". It refuses to run if the full `lumio` application is present.
#
# lumio-lancedb is a SEPARATE package (packages/lumio-lancedb), not a
# lumio-wiki[lancedb] extra — ADR-0010 keeps the downward dependency optional.
# Its base install is Torch-free and gives the LanceDB BM25 retrieval stage.
# Add the [embeddings] extra (sentence-transformers) only if you want a real
# local embedder for semantic/hybrid; tools/eval_lancedb.sh does not need it
# (`lumio-wiki eval --semantic` uses a deterministic offline hash embedder).
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$EXAMPLE_DIR/../.." && pwd)"
VENV="$EXAMPLE_DIR/.venv"
PACKAGE_LANCEDB="$REPO_ROOT/packages/lumio-lancedb"
EMBEDDINGS="${LUMIO_LANCEDB_EMBEDDINGS:-0}"

command -v uv >/dev/null 2>&1 || {
	echo "error: uv is required (https://docs.astral.sh/uv/)" >&2
	exit 1
}

if [[ ! -x "$VENV/bin/python" ]]; then
	echo "error: $VENV not found or incomplete (no bin/python); run ./bootstrap.sh first" >&2
	exit 1
fi

# Refuse if the full `lumio` application is already installed in this venv. The
# "Later" stage must stay a lumio-wiki + lumio-lancedb stack; `lumio` is the
# separate "Finally" stage (#157) and is never layered here.
"$VENV/bin/python" - <<'PY'
from importlib.metadata import PackageNotFoundError, version

try:
	installed = version("lumio")
except PackageNotFoundError:
	print("lumio: not installed (ok)")
else:
	raise SystemExit(
		f"error: the full `lumio` application ({installed}) is installed in this venv. "
		"The Later stage compares lumio-wiki + lumio-lancedb only; `lumio` is the separate "
		"Finally stage (#157). Recreate the venv with ./bootstrap.sh before layering lumio-lancedb."
	)
PY

# Confirm lumio-wiki is present before layering the adapter on top.
"$VENV/bin/python" - <<'PY'
from importlib.metadata import PackageNotFoundError, version

try:
	print(f"lumio-wiki {version('lumio-wiki')}")
except PackageNotFoundError:
	raise SystemExit(
		"error: lumio-wiki is not installed in this venv. Run ./bootstrap.sh first."
	)
PY

# Install the optional LanceDB adapter into the existing venv (no --clear): the
# base lumio-wiki[documents] install is preserved, only the adapter is added.
INSTALL_SPEC="$PACKAGE_LANCEDB"
if [[ "$EMBEDDINGS" == "1" ]]; then
	INSTALL_SPEC="${PACKAGE_LANCEDB}[embeddings]"
fi
echo "Installing $INSTALL_SPEC into the existing venv (base install preserved)..."
uv pip install --python "$VENV/bin/python" "$INSTALL_SPEC"

# Verify the adapter imports and lumio-wiki sees it (doctor prints extra[lancedb]).
"$VENV/bin/python" - <<'PY'
from importlib.metadata import PackageNotFoundError, version

try:
	print(f"lumio-lancedb {version('lumio-lancedb')}")
except PackageNotFoundError:
	raise SystemExit("error: lumio-lancedb did not install")

# Mirror retrieval_eval.lancedb_available(): the adapter needs lumio_lancedb,
# lancedb, and pyarrow all importable. Probed with find_spec (never an import)
# so we report availability without pulling the modules into this process.
import importlib.util

missing = [
	name for name in ("lumio_lancedb", "lancedb", "pyarrow")
	if importlib.util.find_spec(name) is None
]
if missing:
	raise SystemExit(f"error: lumio-lancedb installed but modules missing: {', '.join(missing)}")
print("lancedb adapter: importable (lumio_lancedb, lancedb, pyarrow present)")
PY

"$VENV/bin/lumio-wiki" doctor

printf '\nLater stage ready. lumio-lancedb layered onto the base install.\n'
printf 'Run the comparison with:\n  bash tools/eval_lancedb.sh\n'
