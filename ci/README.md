# CI: the vgi-pdf worker integration suite

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs the unit tests
and this repo's sqllogictest suite (`test/sql/*.test`) against the vgi-pdf VGI
worker through the **real DuckDB `vgi` extension** on every push / PR.

## How it works (no C++ build)

CI drives a **prebuilt** standalone `haybarn-unittest` and installs the
**signed** `vgi` extension from the Haybarn community channel:

1. **Install the worker** — `uv sync --frozen`. `pdf_worker.py` is a PEP 723
   stdio worker spawned via `uv run pdf_worker.py`.
2. **Download the runner** — the matching `haybarn_unittest-*` asset per platform.
3. **Preprocess** — [`preprocess-require.awk`](preprocess-require.awk) injects a
   signed `INSTALL vgi FROM community;` before each bare `LOAD vgi;`.
4. **Run** — [`run-integration.sh`](run-integration.sh) stages the preprocessed
   tree (including the `test/sql/data/*.pdf` fixtures the tests read by relative
   path), points `VGI_PDF_WORKER` at `uv run pdf_worker.py`, and runs the suite.

## Run it locally

```bash
uv sync --python 3.13
HAYBARN_UNITTEST=/path/to/haybarn-unittest \
VGI_PDF_WORKER="uv run --python 3.13 pdf_worker.py" \
  ci/run-integration.sh
```
