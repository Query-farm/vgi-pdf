# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http]>=0.8.5",
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

from vgi_pdf.meta import keywords_json
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
    "# PDF Structure & Rendering for DuckDB\n\n"
    "Query the **structure, layout, and coordinates of PDF documents directly in SQL** -- extract "
    "tables, per-word bounding boxes, page geometry, AcroForm field values, and document metadata, "
    "check encryption status, and render any page to a PNG image, all streamed to DuckDB over Apache "
    "Arrow. This is the layout-and-coordinates companion to plain-text extraction: where a text "
    "extractor flattens a document to a string, `pdf` preserves *where* every word, cell, and box "
    "sits on the page.\n\n"
    "This extension is for data engineers, analysts, and PDF-mining pipelines who need to pull "
    "structured data out of reports, invoices, statements, and scanned forms without leaving SQL. "
    "It is backed entirely by permissive, battle-tested open-source libraries. Table detection and "
    "word geometry come from [pdfplumber]"
    "(https://github.com/jsvine/pdfplumber) ([docs](https://github.com/jsvine/pdfplumber#readme)); "
    "high-fidelity page rasterization is powered by [pypdfium2]"
    "(https://github.com/pypdfium2-team/pypdfium2) "
    "([docs](https://pypdfium2.readthedocs.io/)), Google's Chromium PDFium engine; document "
    "metadata, encryption detection, and AcroForm field reading use [pikepdf]"
    "(https://github.com/pikepdf/pikepdf) ([docs](https://pikepdf.readthedocs.io/)); and rendered "
    "bitmaps are encoded to PNG with [Pillow](https://github.com/python-pillow/Pillow) "
    "([docs](https://pillow.readthedocs.io/)). Deliberately **never PyMuPDF** -- only permissively "
    "licensed dependencies.\n\n"
    "The function surface mirrors the two natural shapes of PDF data. **Scalar functions** answer one "
    "question per row: `page_count` returns the page total, `is_encrypted` flags password-protected "
    "files, `pdf_metadata` returns the Title/Author/Producer map, `form_fields` reads filled-in form "
    "values, and `render_page` rasterizes a page to a PNG `BLOB` at a chosen DPI. **Table functions** "
    "expand a single document into many rows: `pages` yields per-page width/height/rotation, `words` "
    "emits every word with its `x0`/`top` bounding box, and `tables` returns detected table cells in "
    "long `(page, row, col, value)` form. Every function accepts the PDF as either a `VARCHAR` "
    "filesystem path or a `BLOB` of raw bytes. Typical queries: mine tabular data with "
    "`SELECT page, \"row\", col, value FROM pdf.main.tables(pdf := 'report.pdf')`, locate text by "
    "coordinate via `pdf.main.words(pdf := 'doc.pdf')`, audit submitted forms with "
    "`pdf.main.form_fields('application.pdf')`, or thumbnail a document using "
    "`pdf.main.render_page('report.pdf', 1)`."
)

_SCHEMA_DESCRIPTION_LLM = (
    "PDF structure-extraction functions: page count, encryption check, document metadata and form "
    "fields, page-to-PNG rendering, and table functions for word boxes, table cells, and page geometry."
)

_SCHEMA_DESCRIPTION_MD = (
    "The `main` schema groups every PDF structure, layout, and rendering function this worker exposes, "
    "all served over Apache Arrow. Scalar functions answer one question per row -- `page_count`, "
    "`is_encrypted`, `pdf_metadata`, and `form_fields` -- while `render_page` rasterizes a single page to "
    "a PNG image. The table functions `pages`, `words`, and `tables` expand a document into many rows: "
    "per-page geometry, per-word bounding boxes, and long-format table cells. Every function accepts the "
    "PDF as a filesystem path or as raw bytes, and reads the document's structure and coordinates rather "
    "than performing plain-text extraction or OCR."
)

_REPO_URL = "https://github.com/Query-farm/vgi-pdf"

# VGI138: vgi.keywords must be a JSON array of strings, not a comma-separated
# string; ``keywords_json`` serializes these lists into that form.
_CATALOG_KEYWORDS = keywords_json(
    [
        "pdf",
        "document",
        "structure",
        "layout",
        "tables",
        "words",
        "bounding box",
        "page count",
        "render",
        "png",
        "metadata",
        "form fields",
        "acroform",
        "encryption",
        "coordinates",
        "geometry",
    ]
)

_SCHEMA_KEYWORDS = keywords_json(
    [
        "pdf",
        "page_count",
        "is_encrypted",
        "pdf_metadata",
        "form_fields",
        "render_page",
        "tables",
        "words",
        "pages",
        "structure",
        "layout",
        "word boxes",
        "geometry",
        "forms",
        "metadata",
        "rendering",
    ]
)

# VGI506 representative, catalog-qualified example queries for the schema.
_SCHEMA_EXAMPLE_QUERIES = (
    "SELECT pdf.main.page_count('report.pdf');\n"
    "SELECT pdf.main.is_encrypted('secret.pdf');\n"
    "SELECT pdf.main.pdf_metadata('report.pdf')['Title'];\n"
    "SELECT pdf.main.form_fields('application.pdf');\n"
    "SELECT pdf.main.render_page('report.pdf', 1);\n"
    "SELECT * FROM pdf.main.pages(pdf := 'report.pdf');\n"
    "SELECT * FROM pdf.main.words(pdf := 'report.pdf') ORDER BY page, top, x0;\n"
    "SELECT page, \"row\", col, value FROM pdf.main.tables(pdf := 'report.pdf');"
)

_CATALOG_TAGS = {
    "vgi.title": "PDF Structure & Rendering",
    "vgi.keywords": _CATALOG_KEYWORDS,
    "vgi.doc_llm": _CATALOG_DESCRIPTION_LLM,
    "vgi.doc_md": _CATALOG_DESCRIPTION_MD,
    "vgi.author": "Query.Farm",
    "vgi.copyright": "Copyright 2026 Query Farm LLC - https://query.farm",
    "vgi.license": "MIT",
    "vgi.support_contact": f"{_REPO_URL}/issues",
    "vgi.support_policy_url": f"{_REPO_URL}/blob/main/README.md",
}

_SCHEMA_TAGS = {
    "vgi.title": "PDF — main",
    "vgi.keywords": _SCHEMA_KEYWORDS,
    # VGI123 classifying tags use BARE keys (not vgi.-namespaced) for faceting.
    "domain": "documents",
    "category": "parsing",
    "topic": "pdf-structure-extraction",
    # VGI139: no per-object vgi.source_url -- the source link lives only on the
    # catalog object (set via Catalog(source_url=...)).
    "vgi.example_queries": _SCHEMA_EXAMPLE_QUERIES,
    "vgi.doc_llm": _SCHEMA_DESCRIPTION_LLM,
    "vgi.doc_md": _SCHEMA_DESCRIPTION_MD,
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
