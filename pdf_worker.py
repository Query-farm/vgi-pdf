# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http]>=0.16.0",
#     "pdfplumber>=0.11",
#     "pypdfium2>=4.30",
#     "pikepdf>=9",
#     "Pillow>=10",
#     "pyarrow",
# ]
# ///
"""Repo-root stdio/HTTP entry point for the vgi-pdf worker (thin PEP 723 shim).

The worker catalog, ``PdfWorker`` class, and ``main()`` live in the
wheel-importable ``vgi_pdf.worker`` module so the published wheel is a complete,
runnable worker. This repo-root script re-exports them and carries the PEP 723
inline dependency block so ``uv run pdf_worker.py`` keeps working unchanged for
the Makefile, ``ci/run-integration.sh``, and the pytest fixtures.

Usage:
    uv run pdf_worker.py            # serve over stdio (DuckDB subprocess)
    uv run pdf_worker.py --http     # serve over HTTP

    INSTALL vgi FROM community; LOAD vgi;
    ATTACH 'pdf' (TYPE vgi, LOCATION 'uv run pdf_worker.py');
"""

from __future__ import annotations

from vgi_pdf.worker import PdfWorker, main

__all__ = ["PdfWorker", "main"]


if __name__ == "__main__":
    main()
