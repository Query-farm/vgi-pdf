# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http]>=0.8.3",
#     "pdfplumber>=0.11",
#     "pypdfium2>=4.30",
#     "pikepdf>=9",
#     "Pillow>=10",
#     "pyarrow",
# ]
# ///
"""VGI worker exposing PDF *structure* extraction to SQL.

Assembles the functions in ``vgi_pdf`` into a single ``pdf`` catalog and runs
the worker over stdio (DuckDB subprocess) or HTTP. It extracts layout/structure
from PDFs -- page counts, geometry, word bounding boxes, tables, form fields,
metadata, encryption status -- and renders pages to PNG, as DuckDB functions.

This is deliberately NOT vgi-tika: tika does plain text; vgi-pdf does
layout / tables / coordinates / rendering. Backed by permissive libraries only
(pdfplumber MIT, pypdfium2 Apache-2.0, pikepdf MPL-2.0) -- never PyMuPDF/fitz,
which is AGPL/commercial (see the README "Why not PyMuPDF" note).

Usage:
    uv run pdf_worker.py            # serve over stdio (DuckDB subprocess)

    INSTALL vgi FROM community; LOAD vgi;
    ATTACH 'pdf' (TYPE vgi, LOCATION 'uv run pdf_worker.py');

    SELECT pdf.page_count('doc.pdf');                 -- 3
    SELECT pdf.is_encrypted('doc.pdf');               -- false
    SELECT pdf.pdf_metadata('doc.pdf')['Title'];      -- 'Quarterly Report'
    SELECT pdf.form_fields('form.pdf');               -- MAP{...}
    SELECT pdf.render_page('doc.pdf', 1);             -- PNG BLOB
    SELECT * FROM pdf.tables('doc.pdf');              -- long-format cells
    SELECT * FROM pdf.words('doc.pdf') ORDER BY top;  -- word boxes
    SELECT * FROM pdf.pages('doc.pdf');               -- page geometry
"""

from __future__ import annotations

from vgi import Worker
from vgi.catalog import Catalog, Schema

from vgi_pdf.scalars import SCALAR_FUNCTIONS
from vgi_pdf.tables import TABLE_FUNCTIONS

_FUNCTIONS: list[type] = [
    *SCALAR_FUNCTIONS,
    *TABLE_FUNCTIONS,
]

_PDF_CATALOG = Catalog(
    name="pdf",
    default_schema="main",
    schemas=[
        Schema(
            name="main",
            comment="Extract PDF structure: tables, word boxes, geometry, forms, metadata, rendering",
            functions=list(_FUNCTIONS),
        ),
    ],
)


class PdfWorker(Worker):
    """Worker process hosting the ``pdf`` catalog."""

    catalog = _PDF_CATALOG


def main() -> None:
    """Run the pdf worker process (stdio or, via flags, HTTP)."""
    PdfWorker.main()


if __name__ == "__main__":
    main()
