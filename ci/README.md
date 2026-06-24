# CI: the vgi-pdf worker integration suite

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs the unit tests
and this repo's sqllogictest suite (`test/sql/*.test`) against the vgi-pdf VGI
worker through the **real DuckDB `vgi` extension** on every push / PR.

## How it works (no C++ build)

CI drives a **prebuilt** standalone `haybarn-unittest` and installs the
**signed** `vgi` extension from the Haybarn community channel:

1. **Install the worker** — `uv sync --frozen --extra http`. `pdf_worker.py` is a
   PEP 723 stdio worker spawned via `uv run pdf_worker.py`. The `http` extra
   pulls in `waitress` so the worker can also serve over HTTP.
2. **Download the runner** — the matching `haybarn_unittest-*` asset per platform.
3. **Preprocess** — [`preprocess-require.awk`](preprocess-require.awk) injects a
   signed `INSTALL vgi FROM community;` before each bare `LOAD vgi;`.
4. **Run** — [`run-integration.sh`](run-integration.sh) stages the preprocessed
   tree (including the `test/sql/data/*.pdf` fixtures the tests read by relative
   path) and runs the suite over the transport selected by `$TRANSPORT`.

## Transports

The SAME suite runs over three VGI transports, as a CI matrix
(`transport: [subprocess, http, unix]` × `os: [ubuntu, macos]`). The vgi
extension picks the transport from the ATTACH `LOCATION` string that
`run-integration.sh` builds from `$WORKER_CMD` per `$TRANSPORT`:

- **subprocess** (default): `LOCATION` is the bare stdio command
  (`uv run pdf_worker.py`); the extension spawns the worker per query and talks
  Arrow IPC over stdin/stdout.
- **http**: the script boots the worker out-of-band with
  `--http --port 0 --port-file <f>`, waits for the chosen port, and sets
  `LOCATION='http://127.0.0.1:<port>'`. The vgi HTTP transport rides on DuckDB's
  `httpfs`, so the script injects a signed `INSTALL httpfs FROM core; LOAD
  httpfs;` into each staged test for this leg only — **without it ATTACH fails
  with "VGI HTTP transport requires the httpfs extension", and the
  sqllogictest runner's default skip-list silently SKIPs any "HTTP" error,
  reporting a fake pass.** With httpfs loaded the tests actually run.
- **unix**: the script boots the worker with `--unix <sock>`, waits for the
  socket, and sets `LOCATION='unix://<sock>'`.

For http/unix the worker is booted with cwd = the staging dir so it resolves the
staged relative-path PDF fixtures, and is trap-killed on exit.

## Run it locally

```bash
uv sync --python 3.13 --extra http
export PATH="$HOME/.local/bin:$PATH"   # haybarn-unittest lives here
HAYBARN_UNITTEST=$(which haybarn-unittest) TRANSPORT=subprocess ci/run-integration.sh
HAYBARN_UNITTEST=$(which haybarn-unittest) TRANSPORT=http       ci/run-integration.sh
HAYBARN_UNITTEST=$(which haybarn-unittest) TRANSPORT=unix       ci/run-integration.sh
```

Each leg must end with `All tests passed (N ...)` with `N>0`. (The http leg
reports a few more assertions than subprocess/unix because of the injected
`INSTALL httpfs`/`LOAD httpfs` `statement ok` lines.)
