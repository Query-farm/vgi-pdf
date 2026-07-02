# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http]>=0.9.0",
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

import json

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
    "Query the **structure, layout, and coordinates of PDF documents directly in SQL** -- pull tabular "
    "data, per-word geometry, page dimensions, form submissions, and document properties, check whether "
    "a file is encrypted, and rasterize any page to an image, all streamed to DuckDB over Apache Arrow. "
    "Where a plain-text extractor flattens a document to a string, this worker preserves *where* every "
    "word, cell, and box sits on the page.\n\n"
    "## Who it is for\n\n"
    "Built for data engineers, analysts, and document-mining pipelines that need to pull structured "
    "data out of reports, invoices, statements, and scanned forms without leaving SQL. Reach for it "
    "whenever the *position* of content matters -- reconstructing tabular layouts, locating text by "
    "coordinate, auditing filled-in forms, or generating page thumbnails -- and reach for a plain-text "
    "extraction worker instead when you only need the raw prose.\n\n"
    "## Key concepts\n\n"
    "- **Path or bytes.** Inputs are accepted as either a `VARCHAR` filesystem path the worker opens, "
    "or a `BLOB` of raw bytes travelling over Arrow -- so a document already living in a DuckDB column "
    "works as directly as a file on disk.\n"
    "- **PDF points.** All geometry is reported in PDF points (1 pt = 1/72 inch) with the origin at the "
    "top-left corner of the page.\n"
    "- **Long-format output.** Set-returning functions expand one document into many tidy rows, ready "
    "to pivot, filter, and join with ordinary SQL.\n\n"
    "## Backed by permissive libraries only\n\n"
    "Table detection and word geometry come from [pdfplumber]"
    "(https://github.com/jsvine/pdfplumber) ([docs](https://github.com/jsvine/pdfplumber#readme)); "
    "high-fidelity page rasterization is powered by [pypdfium2]"
    "(https://github.com/pypdfium2-team/pypdfium2) "
    "([docs](https://pypdfium2.readthedocs.io/)), Google's Chromium PDFium engine; document properties, "
    "encryption detection, and AcroForm reading use [pikepdf]"
    "(https://github.com/pikepdf/pikepdf) ([docs](https://pikepdf.readthedocs.io/)); and rendered "
    "bitmaps are encoded to PNG with [Pillow](https://github.com/python-pillow/Pillow) "
    "([docs](https://pillow.readthedocs.io/)). Deliberately **never PyMuPDF** -- only permissively "
    "licensed dependencies."
)

_SCHEMA_DESCRIPTION_LLM = (
    "PDF structure-extraction functions: page count, encryption check, document metadata and form "
    "fields, page-to-PNG rendering, and table functions for word boxes, table cells, and page geometry."
)

_SCHEMA_DESCRIPTION_MD = (
    "# PDF Structure -- `main`\n\n"
    "The `main` schema groups every PDF structure, layout, and rendering capability this worker "
    "exposes, all served to DuckDB over Apache Arrow. It reads a document's *structure and coordinates* "
    "-- not its plain text -- so you always know where content sits on the page.\n\n"
    "## What you can do here\n\n"
    "- **Structure** -- count pages and read per-page geometry (width, height, rotation).\n"
    "- **Content** -- pull detected table cells in long format, per-word bounding boxes, and filled-in "
    "form-field values.\n"
    "- **Metadata** -- read the document information dictionary and check encryption status.\n"
    "- **Rendering** -- rasterize a single page to a PNG image at a chosen resolution.\n\n"
    "## Conventions\n\n"
    "Inputs are accepted as either a `VARCHAR` filesystem path or a `BLOB` of raw bytes, and all "
    "coordinates are expressed in PDF points (1/72 inch) measured from the top-left corner. The "
    "set-returning functions take DuckDB `name := value` keyword arguments; the per-row "
    "question-answering functions are ordinary scalars used inline in any projection."
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

# VGI413 controlled-vocabulary category registry for the schema. Ordered JSON
# array of {name, description}; every function carries a matching ``vgi.category``
# tag (set via ``meta.object_tags(category=...)``). Categories drive the worker's
# navigation, listing sections, and SEO descriptions.
_SCHEMA_CATEGORIES = json.dumps(
    [
        {
            "name": "Structure",
            "description": "Page counts and per-page geometry (size, orientation, rotation).",
        },
        {
            "name": "Content",
            "description": ("Data pulled from the page: table cells, per-word bounding boxes, and form-field values."),
        },
        {
            "name": "Metadata",
            "description": "Document information dictionary and encryption status.",
        },
        {
            "name": "Rendering",
            "description": "Rasterize a page to a PNG image.",
        },
    ],
    separators=(",", ":"),
)

# VGI152/VGI920 fixed analyst-task suite. ``vgi-lint simulate`` runs an LLM
# analyst against each prompt (seeing only the catalog metadata, never the
# reference), then grades its answer against ``reference_sql``. Prompts are
# self-contained and target the committed fixtures by path (resolved from the
# worker cwd = repo root). ``ignore_column_names`` keeps grading on the values,
# not on whatever alias the analyst happens to pick for a scalar result.
_AGENT_TEST_TASKS = json.dumps(
    [
        {
            "name": "count_pages",
            "prompt": ("How many pages does the PDF document at the path 'test/sql/data/multipage.pdf' have?"),
            "reference_sql": "SELECT pdf.page_count('test/sql/data/multipage.pdf')",
            "ignore_column_names": True,
        },
        {
            "name": "document_title",
            "prompt": ("Read the document Title recorded in the metadata of the PDF at 'test/sql/data/meta.pdf'."),
            "reference_sql": "SELECT pdf.pdf_metadata('test/sql/data/meta.pdf')['Title']",
            "ignore_column_names": True,
        },
        {
            "name": "is_encrypted",
            "prompt": "Is the PDF at 'test/sql/data/form.pdf' encrypted?",
            "reference_sql": "SELECT pdf.is_encrypted('test/sql/data/form.pdf')",
            "ignore_column_names": True,
        },
        {
            "name": "form_field_value",
            "prompt": (
                "The fillable PDF at 'test/sql/data/form.pdf' has an AcroForm field named 'full_name'. "
                "What value is stored in it?"
            ),
            "reference_sql": "SELECT pdf.form_fields('test/sql/data/form.pdf')['full_name']",
            "ignore_column_names": True,
        },
        {
            "name": "table_cell_count",
            "prompt": ("How many table cells are detected in the PDF at 'test/sql/data/table.pdf'?"),
            "reference_sql": "SELECT count(*) FROM pdf.main.tables(pdf := 'test/sql/data/table.pdf')",
            "ignore_column_names": True,
        },
        {
            "name": "words_on_page",
            "prompt": ("How many words are on page 1 of the PDF at 'test/sql/data/words.pdf'?"),
            "reference_sql": ("SELECT count(*) FROM pdf.main.words(pdf := 'test/sql/data/words.pdf', page := 1)"),
            "ignore_column_names": True,
        },
    ],
    separators=(",", ":"),
)

_CATALOG_TAGS = {
    "vgi.title": "PDF Structure & Rendering",
    "vgi.agent_test_tasks": _AGENT_TEST_TASKS,
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
    "vgi.categories": _SCHEMA_CATEGORIES,
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
