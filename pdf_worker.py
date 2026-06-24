# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http]>=0.8.4",
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
    SELECT * FROM pdf.tables(pdf := 'doc.pdf');             -- long-format cells
    SELECT * FROM pdf.words(pdf := 'doc.pdf') ORDER BY top; -- word boxes
    SELECT * FROM pdf.pages(pdf := 'doc.pdf');              -- page geometry
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

_CATALOG_DESCRIPTION_LLM = (
    "Extract structure and layout from PDF documents in SQL: count pages, detect encryption, read "
    "document metadata (Title/Author/Producer/...) and AcroForm field values, render a page to a PNG "
    "image, and -- as table functions -- pull per-word bounding boxes, long-format table cells, and "
    "per-page geometry (width/height/rotation). Accepts a PDF as a VARCHAR filesystem path or a BLOB of "
    "raw bytes. This is layout/coordinates/tables/rendering, NOT plain-text extraction (use vgi-tika for "
    "text). Use it to mine tables out of reports, locate words by coordinate, inspect form submissions, "
    "or thumbnail PDFs."
)

_CATALOG_DESCRIPTION_MD = (
    "# pdf\n\n"
    "Extract **PDF structure** -- tables, word bounding boxes, page geometry, form fields, metadata, "
    "encryption status -- and **render pages to PNG**, over Apache Arrow. Layout and coordinates, not "
    "plain text.\n\n"
    "**Scalars:** `page_count`, `is_encrypted`, `pdf_metadata`, `form_fields`, `render_page`.\n"
    "**Table functions:** `tables`, `words`, `pages`.\n\n"
    "Every function takes the PDF as a VARCHAR path or a BLOB of raw bytes. Backed by permissive "
    "libraries only (pdfplumber, pypdfium2, pikepdf, Pillow)."
)

_SCHEMA_DESCRIPTION_LLM = (
    "PDF structure-extraction functions: page count, encryption check, document metadata and form "
    "fields, page-to-PNG rendering, and table functions for word boxes, table cells, and page geometry."
)

_SCHEMA_DESCRIPTION_MD = (
    "PDF structure, layout, and rendering functions over Apache Arrow "
    "(tables, word boxes, geometry, forms, metadata, page rendering)."
)

_REPO_URL = "https://github.com/Query-farm/vgi-pdf"

_CATALOG_TAGS = {
    "vgi.description_llm": _CATALOG_DESCRIPTION_LLM,
    "vgi.description_md": _CATALOG_DESCRIPTION_MD,
    "vgi.author": "Query.Farm",
    "vgi.copyright": "Copyright 2026 Query Farm LLC - https://query.farm",
    "vgi.license": "MIT",
    "vgi.support_contact": f"{_REPO_URL}/issues",
    "vgi.support_policy_url": f"{_REPO_URL}/blob/main/README.md",
}

_SCHEMA_TAGS = {
    "vgi.description_llm": _SCHEMA_DESCRIPTION_LLM,
    "vgi.description_md": _SCHEMA_DESCRIPTION_MD,
}

_PDF_CATALOG = Catalog(
    name="pdf",
    default_schema="main",
    comment="Extract PDF structure (tables, word boxes, geometry, forms, metadata) and render pages to PNG.",
    tags=_CATALOG_TAGS,
    source_url=_REPO_URL,
    schemas=[
        Schema(
            name="main",
            comment="Extract PDF structure: tables, word boxes, geometry, forms, metadata, rendering",
            tags=_SCHEMA_TAGS,
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
