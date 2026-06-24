#!/usr/bin/env bash
# Copyright 2026 Query Farm LLC - https://query.farm
#
# Run this repo's sqllogictest suite (test/sql/*.test) against the vgi-pdf
# VGI worker, using a prebuilt standalone `haybarn-unittest` and the signed
# community `vgi` extension — no C++ build from source. See ci/README.md.
#
# The SAME suite is exercised over three VGI transports, selected by $TRANSPORT.
# The vgi extension picks the transport from the LOCATION string the .test files
# ATTACH (`${VGI_PDF_WORKER}`):
#
#   subprocess : a bare stdio command (`uv run pdf_worker.py`) — the
#                extension spawns the worker per query and talks Arrow IPC over
#                stdin/stdout. Default; current behavior.
#   http       : the worker is started out-of-band in `--http` mode on an auto
#                port; LOCATION becomes `http://127.0.0.1:<port>`.
#   unix       : the worker is started out-of-band on an AF_UNIX socket;
#                LOCATION becomes `unix:///path/to.sock`.
#
# Required environment:
#   HAYBARN_UNITTEST     path to the haybarn-unittest binary
#   TRANSPORT            subprocess | http | unix (default: subprocess)
#   WORKER_CMD           the stdio command that runs the worker. Used directly
#                        as the LOCATION for subprocess, and as the process to
#                        boot the server for http/unix. Defaults to
#                        `uv run --python 3.13 <repo>/pdf_worker.py`.
# Optional:
#   STAGE                scratch dir for the preprocessed test tree (default: mktemp)
set -euo pipefail

: "${HAYBARN_UNITTEST:?path to the haybarn-unittest binary}"

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
STAGE="${STAGE:-$(mktemp -d)}"
TRANSPORT="${TRANSPORT:-subprocess}"
WORKER_CMD="${WORKER_CMD:-uv run --python 3.13 $REPO/pdf_worker.py}"

echo "Staging preprocessed tests into $STAGE ..."
mkdir -p "$STAGE/test/sql"
for f in "$REPO"/test/sql/*.test; do
  awk -f "$HERE/preprocess-require.awk" "$f" > "$STAGE/test/sql/$(basename "$f")"
done

# This repo's tests read committed fixtures by relative path (test/sql/data/*),
# and the runner cd's into the stage; stage those fixtures alongside the tests.
# For http/unix the WORKER is booted out-of-band (below) with cwd = $STAGE, so
# it resolves these staged relative-path fixtures too.
if [ -d "$REPO/test/sql/data" ]; then
  mkdir -p "$STAGE/test/sql/data"
  cp -R "$REPO"/test/sql/data/. "$STAGE/test/sql/data/"
fi

# ---------------------------------------------------------------------------
# Per-transport: resolve VGI_PDF_WORKER (the LOCATION) and, for the
# out-of-band transports, boot the worker server + arrange trap-cleanup.
# ---------------------------------------------------------------------------
SERVER_PID=""
SOCK=""
PORT_FILE=""

cleanup() {
  rc=$?
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  if [[ -n "$SOCK" ]]; then rm -f "$SOCK"; fi
  if [[ -n "$PORT_FILE" ]]; then rm -f "$PORT_FILE"; fi
  # Always succeed: an EXIT trap whose last command returns non-zero (e.g. a
  # short-circuited `[[ -n "" ]] && …` when nothing needs cleaning) would
  # otherwise become the script's exit status under `set -e` and fail an
  # already-passing run. Preserve the real exit code captured above.
  return "$rc"
}
trap cleanup EXIT

case "$TRANSPORT" in
  subprocess)
    export VGI_PDF_WORKER="$WORKER_CMD"
    ;;

  http)
    # The vgi extension's HTTP transport is implemented on top of DuckDB's
    # httpfs extension, so an `http://` ATTACH binds with
    #   "Binder Error: VGI HTTP transport requires the httpfs extension."
    # unless httpfs is loaded first. (The haybarn sqllogictest runner's default
    # skip list swallows any error containing "HTTP", so without this the whole
    # suite would silently SKIP rather than fail — a fake pass.) The .test files
    # are transport-agnostic; inject a signed `INSTALL httpfs FROM core; LOAD
    # httpfs;` right after the awk-injected `LOAD vgi;` in each staged file, so
    # httpfs is present only when we actually run over HTTP.
    echo "Injecting httpfs load into staged tests (HTTP transport needs it) ..."
    for sf in "$STAGE"/test/sql/*.test; do
      awk '
        { print }
        /^LOAD[ \t]+vgi[ \t]*;[ \t]*$/ && !done {
          print "";
          print "statement ok";
          print "INSTALL httpfs FROM core;";
          print "";
          print "statement ok";
          print "LOAD httpfs;";
          done = 1
        }
      ' "$sf" > "$sf.tmp" && mv "$sf.tmp" "$sf"
    done

    # Boot the worker in HTTP mode on an auto-selected port. The worker writes
    # the chosen port to --port-file atomically (tmp + rename), so we watch for
    # the file to appear rather than parsing stdout. HTTP mode needs the `http`
    # extra (waitress); WORKER_CMD must resolve it — CI installs the http extra
    # and the PEP 723 header lists it too. cwd = $STAGE so the worker resolves
    # the staged relative-path fixtures.
    PORT_FILE="$(mktemp -u "${TMPDIR:-/tmp}/pdf-port.XXXXXX")"
    LOG_FILE="${TMPDIR:-/tmp}/pdf-http-server.log"
    echo "Starting HTTP worker: $WORKER_CMD --http --port 0 --port-file $PORT_FILE"
    # shellcheck disable=SC2086
    ( cd "$STAGE" && $WORKER_CMD --http --port 0 --port-file "$PORT_FILE" ) > "$LOG_FILE" 2>&1 &
    SERVER_PID=$!

    PORT=""
    for _ in $(seq 1 120); do
      if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "ERROR: HTTP worker exited before reporting a port. Log:" >&2
        cat "$LOG_FILE" >&2
        exit 1
      fi
      if [[ -s "$PORT_FILE" ]]; then
        PORT="$(tr -d '[:space:]' < "$PORT_FILE")"
        [[ -n "$PORT" ]] && break
      fi
      sleep 0.5
    done
    if [[ -z "$PORT" ]]; then
      echo "ERROR: timed out waiting for HTTP worker port-file. Log:" >&2
      cat "$LOG_FILE" >&2
      exit 1
    fi
    echo "HTTP worker ready on port $PORT (pid $SERVER_PID)"
    export VGI_PDF_WORKER="http://127.0.0.1:$PORT"
    ;;

  unix)
    # Boot the worker bound to an AF_UNIX socket. We poll for the socket file to
    # appear. cwd = $STAGE so the worker resolves staged relative-path fixtures.
    SOCK="${TMPDIR:-/tmp}/pdf-$$.sock"
    rm -f "$SOCK"
    LOG_FILE="${TMPDIR:-/tmp}/pdf-unix-server.log"
    echo "Starting unix worker: $WORKER_CMD --unix $SOCK"
    # shellcheck disable=SC2086
    ( cd "$STAGE" && $WORKER_CMD --unix "$SOCK" ) > "$LOG_FILE" 2>&1 &
    SERVER_PID=$!

    READY=""
    for _ in $(seq 1 120); do
      if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "ERROR: unix worker exited before binding the socket. Log:" >&2
        cat "$LOG_FILE" >&2
        exit 1
      fi
      if [[ -S "$SOCK" ]]; then
        READY=1
        break
      fi
      sleep 0.5
    done
    if [[ -z "$READY" ]]; then
      echo "ERROR: timed out waiting for unix worker socket. Log:" >&2
      cat "$LOG_FILE" >&2
      exit 1
    fi
    echo "unix worker ready on $SOCK (pid $SERVER_PID)"
    export VGI_PDF_WORKER="unix://$SOCK"
    ;;

  *)
    echo "ERROR: unknown TRANSPORT '$TRANSPORT' (want subprocess|http|unix)" >&2
    exit 2
    ;;
esac

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
echo "Running suite (transport: $TRANSPORT, worker: $VGI_PDF_WORKER) ..."
"$HAYBARN_UNITTEST" "test/sql/*"
