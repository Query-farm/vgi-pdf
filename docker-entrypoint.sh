#!/bin/sh
# Dispatch the vgi-pdf image into one of its transports:
#   http   (default) HTTP server on $PORT (vgi-serve --http: /health + VGI RPC)
#   stdio            a worker DuckDB spawns over stdio (on-host execution)
#   *                exec'd verbatim (debug escape hatch)
set -e
case "${1:-http}" in
  http)  exec vgi-serve vgi_pdf.worker:PdfWorker --http --host 0.0.0.0 --port "${PORT:-8000}" ;;
  stdio) shift 2>/dev/null || true; exec vgi-pdf-worker "$@" ;;
  *)     exec "$@" ;;
esac
