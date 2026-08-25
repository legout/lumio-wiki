#!/usr/bin/env bash
# Download the exact TestPyPI release wheels and verify them against the
# recorded build hashes (issue #181). Shared by the release workflow's
# TestPyPI verification jobs and the PyPI promotion job so both prove byte
# identity with the build record the same way — the promotion certifies the
# artifacts TestPyPI is actually serving, never a rebuild.
#
# Usage: fetch_verified_testpypi_wheels.sh DEST HASHES_FILE VERSION
#   DEST        directory the three wheels are downloaded into
#   HASHES_FILE sha256sum file recorded by the release build job
#   VERSION     lockstep release version (e.g. 0.1.1)
set -euo pipefail

dest=$1
hashes=$(realpath "$2")
version=$3

uv venv --seed --python 3.14 /tmp/fetch-testpypi-venv
mkdir -p "$dest"
# TestPyPI propagation after upload can lag; retry briefly before failing.
for attempt in $(seq 1 12); do
  if /tmp/fetch-testpypi-venv/bin/pip download --no-deps \
       --index-url https://test.pypi.org/simple/ \
       --extra-index-url https://pypi.org/simple/ \
       -d "$dest" "lumio-wiki==$version" "lumio-lancedb==$version" "lumio==$version"; then
    break
  fi
  echo "TestPyPI not serving the release yet (attempt $attempt); retrying" >&2
  sleep 30
done
test "$(find "$dest" -maxdepth 1 -name '*.whl' | wc -l)" -eq 3
(cd "$dest" && sha256sum -c "$hashes")
