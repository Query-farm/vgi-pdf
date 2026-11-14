"""VGI worker exposing PDF *structure* extraction to SQL (wheel-importable).

Assembles the functions in ``vgi_pdf`` into a single ``pdf`` catalog and runs
the worker over stdio (DuckDB subprocess) or HTTP. It extracts layout/structure
from PDFs -- page counts, geometry, word bounding boxes, tables, form fields,
metadata, encryption status -- and renders pages to PNG, as DuckDB functions.

This is deliberately NOT vgi-tika: tika does plain text; vgi-pdf does
layout / tables / coordinates / rendering. Backed by permissive libraries only
(pdfplumber MIT, pypdfium2 Apache-2.0, pikepdf MPL-2.0) -- never PyMuPDF/fitz,
which is AGPL/commercial (see the README "Why not PyMuPDF" note).

The catalog, ``PdfWorker`` class, and ``main()`` live here (in the installable
package) so the published wheel is a complete, runnable worker. The repo-root
``pdf_worker.py`` is a thin PEP 723 shim that re-exports them.

Usage:
    vgi-pdf-worker                  # serve over stdio (installed console script)
    vgi-serve vgi_pdf.worker:PdfWorker --http --host 0.0.0.0 --port 8000

    INSTALL vgi FROM community; LOAD vgi;
    ATTACH 'pdf' (TYPE vgi, LOCATION 'vgi-pdf-worker');

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

from vgi_pdf.core import FIXTURES_URL, fixture_blob, fixture_set
from vgi_pdf.meta import keywords_json
from vgi_pdf.scalars import PDF_INVENTORY_EXAMPLE, SCALAR_FUNCTIONS
from vgi_pdf.tables import COMPOSED_EXAMPLES, FUNCTIONS_VIEW, TABLE_FUNCTIONS, example_query

_FUNCTIONS: list[type] = [
    *SCALAR_FUNCTIONS,
    *TABLE_FUNCTIONS,
]

_CATALOG_DESCRIPTION_LLM = (
    "Extract structure and layout from PDF documents in SQL: count pages, detect encryption, read "
    "document metadata (Title/Author/Producer/...) and AcroForm field values, render a page to a PNG "
    "image, and -- as table functions -- pull per-word bounding boxes, long-format table cells, and "
    "per-page geometry (width/height/rotation). Accepts a PDF as a `VARCHAR` filesystem path or a `BLOB` "
    "of raw bytes. This is layout/coordinates/tables/rendering, NOT plain-text extraction (use vgi-tika for "
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

# VGI506/VGI515 representative, catalog-qualified example queries for the schema.
# A JSON array of {description, sql} so every example carries a human-readable
# description (VGI515). They read the committed fixtures over HTTPS from a pinned
# tag (``read_blob`` DuckDB-side), so they run from any directory without a repo
# checkout: scalars take the bytes inline; table functions take ``pdf :=`` as a
# constant (no subquery allowed), so those examples load the bytes into a session
# variable first and are shown as a two-statement script.
_SCHEMA_EXAMPLE_QUERIES = json.dumps(
    [
        {
            "description": "Count the pages in a PDF file.",
            "sql": f"SELECT pdf.main.page_count({fixture_blob('multipage.pdf')})",
        },
        {
            "description": "Check whether a PDF is encrypted.",
            "sql": f"SELECT pdf.main.is_encrypted({fixture_blob('form.pdf')})",
        },
        {
            "description": "Read the Title from a PDF's document metadata.",
            "sql": f"SELECT pdf.main.pdf_metadata({fixture_blob('meta.pdf')})['Title']",
        },
        {
            "description": "Read a single AcroForm field value by name.",
            "sql": f"SELECT pdf.main.form_fields({fixture_blob('form.pdf')})['full_name']",
        },
        {
            "description": "Render page 1 to a PNG and measure its byte size.",
            "sql": f"SELECT octet_length(pdf.main.render_page({fixture_blob('multipage.pdf')}, 1))",
        },
        {
            "description": "List each page's width and height.",
            "sql": (
                f"{fixture_set('multipage.pdf')};\n"
                "SELECT page, width, height FROM pdf.main.pages(pdf := getvariable('doc'))"
            ),
        },
        {
            "description": "Extract word text in reading order.",
            "sql": (
                f"{fixture_set('words.pdf')};\n"
                "SELECT page, text FROM pdf.main.words(pdf := getvariable('doc')) ORDER BY page, top, x0"
            ),
        },
        {
            "description": "Pull detected table cells in long format.",
            "sql": (
                f"{fixture_set('table.pdf')};\n"
                "SELECT page, \"row\", col, value FROM pdf.main.tables(pdf := getvariable('doc'))"
            ),
        },
        {
            "description": "Browse the worker's object registry.",
            "sql": "SELECT name, kind, category FROM pdf.main.functions ORDER BY category, name",
        },
        # Composed examples (several of the worker's objects in one query); the SQL
        # is defined once beside the object that drives it and shared from there.
        {"description": PDF_INVENTORY_EXAMPLE[0], "sql": PDF_INVENTORY_EXAMPLE[1]},
        *(example_query(entry) for entry in COMPOSED_EXAMPLES),
    ],
    separators=(",", ":"),
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
        {
            "name": "Discovery",
            "description": "Browsable registry of the objects this worker exposes.",
        },
    ],
    separators=(",", ":"),
)

# VGI152/VGI920 fixed analyst-task suite. ``vgi-lint simulate`` runs an LLM
# analyst against each prompt (seeing only the catalog metadata). Each task is a
# public {name, prompt} object; prompts are self-contained and target the
# committed fixtures by their pinned HTTPS URL, so they need no repo checkout.
# VGI416: the grader-only fields (``reference_sql``, ``ignore_column_names``) live
# in the private ``vgi-agent-tests.yaml`` sidecar at the repo root, never in metadata.
_AGENT_TEST_TASKS = json.dumps(
    [
        {
            "name": "count_pages",
            "prompt": (f"How many pages does the PDF document at '{FIXTURES_URL}/multipage.pdf' have?"),
        },
        {
            "name": "document_title",
            "prompt": (f"Read the document Title recorded in the metadata of the PDF at '{FIXTURES_URL}/meta.pdf'."),
        },
        {
            "name": "is_encrypted",
            "prompt": f"Is the PDF at '{FIXTURES_URL}/form.pdf' encrypted?",
        },
        {
            "name": "form_field_value",
            "prompt": (
                f"The fillable PDF at '{FIXTURES_URL}/form.pdf' has an AcroForm field named 'full_name'. "
                "What value is stored in it?"
            ),
        },
        {
            "name": "table_cell_count",
            "prompt": (f"How many table cells are detected in the PDF at '{FIXTURES_URL}/table.pdf'?"),
        },
        {
            "name": "words_on_page",
            "prompt": (f"How many words are on page 1 of the PDF at '{FIXTURES_URL}/words.pdf'?"),
        },
        {
            "name": "widest_page_width",
            "prompt": (
                f"For the PDF at '{FIXTURES_URL}/multipage.pdf', report the width in PDF points of its widest page."
            ),
        },
        {
            "name": "render_produces_png",
            "prompt": (
                f"Render page 1 of the PDF at '{FIXTURES_URL}/multipage.pdf' to a PNG image. Does the "
                "call return a non-empty image (more than zero bytes)? Answer true or false."
            ),
        },
        {
            "name": "words_in_top_quarter",
            "prompt": (
                f"In the PDF at '{FIXTURES_URL}/table.pdf', how many words sit in the top quarter of their "
                "page, i.e. the word's top edge is less than 25% of that page's height? Answer with a single number."
            ),
        },
        {
            "name": "pages_by_author",
            "prompt": (
                f"Consider these four PDFs: '{FIXTURES_URL}/multipage.pdf', '{FIXTURES_URL}/meta.pdf', "
                f"'{FIXTURES_URL}/table.pdf', and '{FIXTURES_URL}/form.pdf'. What is the total number of pages "
                "across the documents whose metadata Author is 'Ada Lovelace'? Answer with a single number."
            ),
        },
        {
            "name": "count_worker_objects",
            "prompt": (
                "This worker publishes a discovery registry of the objects it exposes. How many objects "
                "does that registry list?"
            ),
        },
    ],
    separators=(",", ":"),
)

_CATALOG_TAGS = {
    "vgi.title": "PDF Structure & Rendering",
    "vgi.subtitle": (
        "Pull tables, words with their positions, page geometry, form fields and metadata out of PDFs, "
        "and render pages to PNG."
    ),
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
    "vgi.title": "PDF Functions",
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
            path=["main"],
            comment="Extract PDF structure: tables, word boxes, geometry, forms, metadata, rendering",
            tags=_SCHEMA_TAGS,
            functions=list(_FUNCTIONS),
            views=[FUNCTIONS_VIEW],
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
