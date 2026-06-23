#!/usr/bin/env bash
# Copyright 2026 Query Farm LLC - https://query.farm
#
# Run this repo's sqllogictest suite (test/sql/*.test) against the vgi-pdf
# VGI worker, using a prebuilt standalone `haybarn-unittest` and the signed
# community `vgi` extension — no C++ build from source. See ci/README.md.
#
# Required environment:
#   HAYBARN_UNITTEST       path to the haybarn-unittest binary
#   VGI_PDF_WORKER  worker LOCATION the .test files ATTACH (a stdio command
#                          such as `uv run pdf_worker.py`, or an http:// URL)
# Optional:
#   STAGE                  scratch dir for the preprocessed test tree (default: mktemp)
set -euo pipefail

: "${HAYBARN_UNITTEST:?path to the haybarn-unittest binary}"
: "${VGI_PDF_WORKER:?worker LOCATION (stdio command or http:// URL)}"

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
STAGE="${STAGE:-$(mktemp -d)}"

echo "Staging preprocessed tests into $STAGE ..."
mkdir -p "$STAGE/test/sql"
for f in "$REPO"/test/sql/*.test; do
  awk -f "$HERE/preprocess-require.awk" "$f" > "$STAGE/test/sql/$(basename "$f")"
done

# This repo's tests read committed fixtures by relative path (test/sql/data/*),
# and the runner cd's into the stage; stage those fixtures alongside the tests.
if [ -d "$REPO/test/sql/data" ]; then
  mkdir -p "$STAGE/test/sql/data"
  cp -R "$REPO"/test/sql/data/. "$STAGE/test/sql/data/"
fi

cd "$STAGE"

# Warm the extension cache once: vgi from the signed community channel. A miss
# here is only a warning — the per-test INSTALL/LOAD (injected by
# preprocess-require.awk) is what actually gates each file.
echo "Warming the extension cache (vgi from community) ..."
mkdir -p "$STAGE/test"
cat > "$STAGE/test/_warm.test" <<'WARM'
# name: test/_warm.test
# group: [warm]
statement ok
INSTALL vgi FROM community;
WARM
"$HAYBARN_UNITTEST" "test/_warm.test" >/dev/null 2>&1 || echo "::warning::extension warm step did not fully succeed"
rm -f "$STAGE/test/_warm.test"

# Run the whole suite in one invocation, streaming the runner's native
# sqllogictest report. Any failed assertion exits non-zero and fails the job.
echo "Running suite (worker: $VGI_PDF_WORKER) ..."
"$HAYBARN_UNITTEST" "test/sql/*"
