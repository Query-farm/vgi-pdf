"""Set-returning PDF structure table functions for DuckDB.

These expand to **many rows** per PDF, so they are exposed as **table
functions** -- the form that accepts DuckDB ``name := value`` arguments
(``page``). The per-row, single-value PDF functions are *scalars* and live in
:mod:`vgi_pdf.scalars`.

    SELECT * FROM pdf.tables(pdf := 'report.pdf');                 -- every cell, long format
    SELECT * FROM pdf.tables(pdf := 'report.pdf', page := 1);      -- only page 1
    SELECT * FROM pdf.words(pdf := 'report.pdf') ORDER BY top, x0; -- word boxes
    SELECT * FROM pdf.pages(pdf := 'report.pdf');                  -- page geometry

Polymorphic ``pdf`` input
-------------------------
The ``pdf`` argument is **either** a ``VARCHAR`` filesystem path the worker opens
**or** a ``BLOB`` of raw PDF bytes. Unlike the scalars -- which register a
path/bytes pair of *positional* overloads -- a table function that also takes
the optional ``page`` argument cannot overload on the positional type: DuckDB
would render the first parameter as the unnamed ``col0`` placeholder and a named
``pdf := …`` call would be ambiguous across the VARCHAR/BLOB casts. So each
table function declares a **single** ``pdf`` argument typed
:class:`~vgi.arguments.AnyArrowValue` (with a VARCHAR-or-BLOB ``type_bound``) and
dispatches on the runtime value type in :func:`_source_from_any`. Because the
argument is a named table-function parameter, call it by keyword
(``pdf := '…'``); :data:`_PAGE` is the optional ``page :=`` filter.

Hostile input: an unreadable / encrypted / malformed PDF surfaces a clean
DuckDB error (raised from :mod:`vgi_pdf.core`), never a worker crash or hang. A
NULL ``pdf`` argument likewise surfaces a clean ``ArgumentValidationError`` (the
single ``ANY``-typed parameter is required and non-nullable) rather than a crash.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, ClassVar

import pyarrow as pa
from vgi.arguments import AnyArrow, AnyArrowValue, Arg
from vgi.catalog import View
from vgi.metadata import FunctionExample
from vgi.table_function import (
    BindParams,
    ProcessParams,
    TableCardinality,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)
from vgi_rpc import ArrowSerializableDataclass
from vgi_rpc.rpc import OutputCollector

from . import core
from .core import PdfSource
from .meta import object_tags
from .schema_utils import field

_SRC = "vgi_pdf/tables.py"

# ---------------------------------------------------------------------------
# Externalized scan cursor (HTTP-continuation fix)
# ---------------------------------------------------------------------------
# Over the stateless HTTP transport the framework wire-serializes a producer's
# per-scan state after every ``process`` tick and resumes by deserializing it,
# emitting at most one producer batch per response. A position-less ``state:
# None`` generator that emits ALL rows in one ``out.emit`` then finishes would
# restart from row 0 on every HTTP resume and loop forever once the output
# exceeds one producer batch (``words`` is hundreds-thousands of rows per PDF,
# ``tables`` one row per cell -- both genuinely unbounded). subprocess/unix
# keep state in-process so they hide the bug; only http (and the
# serialize-between-ticks unit test) expose it.
#
# Fix: carry an explicit cursor in the serializable ``ScanState`` -- the
# materialized full result batch (as IPC bytes) plus an integer ``offset``.
# Each tick emits a bounded ``ROWS_PER_TICK`` slice from ``offset``, advances
# ``offset``, and finishes when drained. Rows/schema are byte-identical to the
# old emit-all path.

ROWS_PER_TICK = 64  # bounded slice per tick; cursor observable across HTTP limit-1


@dataclass(kw_only=True)
class ScanState(ArrowSerializableDataclass):
    """Externalized scan cursor, round-tripped across every ``process`` tick.

    ``started`` flips once the (possibly heavy) PDF source has been read and the
    full result materialized; ``rows_ipc`` holds those result rows as IPC bytes;
    ``offset`` is the next unemitted row. All fields wire-serialize through the
    HTTP continuation token so a resumed tick sees the advanced offset and emits
    the next slice (or finishes) -- never re-reads the PDF from row 0.

    ``started`` distinguishes "not yet read" from "read an empty/NULL source".
    """

    started: bool = False
    offset: int = 0
    rows_ipc: bytes = b""


def result_to_ipc(batch: pa.RecordBatch) -> bytes:
    """Serialize a single RecordBatch to Arrow IPC stream bytes."""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, batch.schema) as writer:  # type: ignore[no-untyped-call]
        writer.write_batch(batch)
    result: bytes = sink.getvalue().to_pybytes()
    return result


def ipc_to_table(value: bytes) -> pa.Table:
    """Read Arrow IPC stream bytes back into a Table."""
    reader = pa.ipc.open_stream(pa.BufferReader(value))  # type: ignore[no-untyped-call]
    return reader.read_all()


def _stream_slice(state: ScanState, schema: pa.Schema, out: OutputCollector) -> None:
    """Emit one bounded slice from ``state.offset``; finish when drained.

    The materialized full batch lives in ``state.rows_ipc`` (the source of
    truth across the wire). This emits at most ``ROWS_PER_TICK`` rows starting
    at ``state.offset``, advances ``offset``, and calls ``out.finish()`` once
    ``offset >= total`` (an empty result terminates immediately: 0 >= 0).
    """
    table = ipc_to_table(state.rows_ipc)
    total = table.num_rows
    if state.offset >= total:
        out.finish()
        return
    end = min(state.offset + ROWS_PER_TICK, total)
    chunk = table.slice(state.offset, end - state.offset)
    out.emit(chunk.combine_chunks().to_batches()[0])
    state.offset = end


# Optional 1-based page filter shared by ``tables`` and ``words``. NULL means
# "all pages". Explicit ``arrow_type`` so a supplied INTEGER binds correctly
# (without it the ``None`` default makes the SDK infer a NULL Arrow type).
_PAGE = Arg[int | None](
    "page",
    default=None,
    arrow_type=pa.int32(),
    doc="Optional 1-based page number to restrict to (NULL = all pages).",
)


# ---------------------------------------------------------------------------
# The polymorphic ``pdf`` argument (VARCHAR path OR BLOB bytes) as ONE param.
# ---------------------------------------------------------------------------
# Scalars register a path/bytes pair of overloads, but a table function that
# *also* takes the optional named ``page`` arg cannot do that: with two
# positional-type overloads DuckDB renders the first parameter as the unnamed
# placeholder ``col0`` (and a named ``pdf := …`` call would be ambiguous between
# the VARCHAR/BLOB casts). So every structure table function declares a single
# ``pdf`` argument typed :class:`AnyArrowValue` (the ``Arg[AnyArrow]`` subscript
# registers the DuckDB parameter as ``ANY`` and silences the ``type_bound``
# warning; the annotation's ``AnyArrowValue`` base type is what flags the spec as
# any-typed) with a VARCHAR-or-BLOB ``type_bound`` — one named ``ANY`` signature,
# called by keyword (``tables(pdf := 'x')``) and dispatched on the runtime value
# type. A bare positional ``tables('x')`` no longer binds (DuckDB won't coerce a
# VARCHAR/BLOB literal to the single ANY parameter without the keyword), and a
# NULL ``pdf`` is rejected with a clean ``ArgumentValidationError`` (the required
# ANY param cannot be Optional without losing the any-type registration).
_PDF = Arg[AnyArrow](
    "pdf",
    type_bound=[pa.types.is_string, pa.types.is_large_string, pa.types.is_binary, pa.types.is_large_binary],
    doc="The PDF to read: either a filesystem path the worker opens, or the raw PDF bytes themselves.",
)


def _source_from_any(value: object | None) -> PdfSource | None:
    """Build a :class:`PdfSource` from the polymorphic ``pdf`` argument.

    In the typed-dataclass argument pattern the bound value is the raw Python
    value (``str`` / ``bytes``), or an :class:`AnyArrowValue` wrapper when the
    SDK surfaces metadata. Dispatches on the runtime type: ``str`` -> a
    filesystem path, ``bytes`` -> raw PDF bytes. A NULL/absent argument yields
    ``None`` so the caller emits no rows.

    Args:
        value: The bound ``pdf`` argument (raw value or wrapper), or ``None``.

    Returns:
        A normalized source handle, or ``None`` for a NULL/empty input.
    """
    raw = value.value if isinstance(value, AnyArrowValue) else value
    if raw is None:
        return None
    if isinstance(raw, str):
        return PdfSource.from_path(raw)
    if isinstance(raw, bytes | bytearray | memoryview):
        return PdfSource.from_bytes(bytes(raw))
    raise core.PdfError(f"unsupported pdf argument type: {type(raw).__name__}")


# ---------------------------------------------------------------------------
# pages(pdf) -> (page, width, height, rotation)
# ---------------------------------------------------------------------------

_PAGES_SCHEMA = pa.schema(
    [
        field("page", pa.int32(), "1-based page number.", nullable=False),
        field("width", pa.float64(), "Page width in PDF points.", nullable=False),
        field("height", pa.float64(), "Page height in PDF points.", nullable=False),
        field("rotation", pa.int32(), "Page rotation in degrees (0/90/180/270).", nullable=False),
    ]
)


@dataclass(kw_only=True)
class _PagesArgs:
    pdf: Annotated[AnyArrowValue, _PDF]


_PAGES_COLUMNS_MD = (
    "| column | type | description |\n"
    "|---|---|---|\n"
    "| `page` | INTEGER | 1-based page number. |\n"
    "| `width` | DOUBLE | Page width in PDF points. |\n"
    "| `height` | DOUBLE | Page height in PDF points. |\n"
    "| `rotation` | INTEGER | Page rotation in degrees (0/90/180/270). |"
)

# VGI307/VGI414: the structured result schema (replaces the retired free-form
# `vgi.result_columns_md`). JSON array of {name, type, description}.
_PAGES_COLUMNS_SCHEMA = json.dumps(
    [
        {"name": "page", "type": "INTEGER", "description": "1-based page number."},
        {"name": "width", "type": "DOUBLE", "description": "Page width in PDF points (1 pt = 1/72 inch)."},
        {"name": "height", "type": "DOUBLE", "description": "Page height in PDF points (1 pt = 1/72 inch)."},
        {"name": "rotation", "type": "INTEGER", "description": "Page rotation in degrees (0, 90, 180, or 270)."},
    ]
)

_PAGES_TAGS = {
    **object_tags(
        title="List PDF Page Geometry",
        doc_llm=(
            "# pages\n\n"
            "Table function returning **one row per page** of a PDF with its physical geometry: "
            "`page` (1-based), `width` and `height` in PDF points (1 pt = 1/72 inch), and `rotation` in "
            "degrees. Call by keyword: `pages(pdf := '...')`, where `pdf` is a `VARCHAR` path or a "
            "`BLOB` of raw bytes.\n\n"
            "Use it to learn how many pages a document has and each page's size/orientation -- e.g. to "
            "detect landscape vs portrait pages, find oversized pages, or drive per-page rendering.\n\n"
            "**Edge cases:** a NULL `pdf` is rejected with a clean argument error; an unreadable/"
            "encrypted PDF surfaces a clean DuckDB error (not a crash). Output streams in bounded "
            "slices so even very large documents page safely over every transport."
        ),
        doc_md=(
            "# List PDF Page Geometry\n\n"
            "Returns the geometry of every page in a PDF, one row per page: the 1-based page number and "
            "each page's width, height, and rotation.\n\n"
            "## Example\n\n"
            "Calling `pdf.main.pages(pdf := 'report.pdf')` yields one row per page; a US-Letter portrait "
            "page reports a width of `612` and a height of `792` (points) with a rotation of `0`. "
            "Restrict to landscape pages by keeping rows where `width` exceeds `height`. Ready-to-run "
            "SQL lives in the example queries.\n\n"
            "## Columns\n\n" + _PAGES_COLUMNS_MD + "\n\n"
            "## Notes\n\n"
            "Dimensions are in PDF points (1/72 inch). Call the function by keyword (`pdf := ...`). A "
            "NULL `pdf` raises an argument error; an unreadable document raises a clean error."
        ),
        keywords=[
            "pages",
            "page geometry",
            "page size",
            "dimensions",
            "width",
            "height",
            "rotation",
            "orientation",
            "landscape",
        ],
        category="Structure",
        relative_path=_SRC,
    ),
    "vgi.result_columns_schema": _PAGES_COLUMNS_SCHEMA,
}


def _build_pages(src: PdfSource, schema: pa.Schema) -> pa.RecordBatch:
    rows = core.pages(src)
    return pa.RecordBatch.from_pydict(
        {
            "page": [r[0] for r in rows],
            "width": [r[1] for r in rows],
            "height": [r[2] for r in rows],
            "rotation": [r[3] for r in rows],
        },
        schema=schema,
    )


@init_single_worker
@bind_fixed_schema
class PagesFunction(TableFunctionGenerator[_PagesArgs, ScanState]):
    """``pages(pdf)`` -- per-page geometry of a PDF."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = _PAGES_SCHEMA

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "pages"
        description = "Per-page geometry (page, width, height, rotation) of a PDF (VARCHAR path or BLOB bytes)"
        categories = ["pdf", "structure"]
        tags = _PAGES_TAGS
        examples = [
            FunctionExample(
                sql=(
                    "SELECT page, width, height, rotation "
                    "FROM pdf.main.pages(pdf := 'test/sql/data/multipage.pdf') ORDER BY page"
                ),
                description="Per-page geometry of a multi-page PDF, ordered by page",
            ),
            FunctionExample(
                sql=(
                    "SELECT count(*) AS landscape_pages "
                    "FROM pdf.main.pages(pdf := 'test/sql/data/multipage.pdf') WHERE width > height"
                ),
                description="Count the landscape (wider-than-tall) pages",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[_PagesArgs]) -> TableCardinality:
        """Return the estimated output cardinality."""
        return TableCardinality(estimate=10, max=None)

    @classmethod
    def initial_state(cls, params: ProcessParams[_PagesArgs]) -> ScanState:
        """Return a fresh scan-state cursor for a new execution."""
        return ScanState()

    @classmethod
    def process(cls, params: ProcessParams[_PagesArgs], state: ScanState, out: OutputCollector) -> None:
        """Materialize the page batch once, then stream bounded slices."""
        if not state.started:
            src = _source_from_any(params.args.pdf)
            if src is None:
                out.finish()
                return
            state.rows_ipc = result_to_ipc(_build_pages(src, params.output_schema))
            state.started = True
        _stream_slice(state, params.output_schema, out)


# ---------------------------------------------------------------------------
# words(pdf, page := NULL) -> (page, text, x0, top, x1, bottom)
# ---------------------------------------------------------------------------

_WORDS_SCHEMA = pa.schema(
    [
        field("page", pa.int32(), "1-based page number.", nullable=False),
        field("text", pa.string(), "The word's text.", nullable=False),
        field("x0", pa.float64(), "Left edge (PDF points from left).", nullable=False),
        field("top", pa.float64(), "Top edge (PDF points from top).", nullable=False),
        field("x1", pa.float64(), "Right edge (PDF points from left).", nullable=False),
        field("bottom", pa.float64(), "Bottom edge (PDF points from top).", nullable=False),
    ]
)


@dataclass(kw_only=True)
class _WordsArgs:
    pdf: Annotated[AnyArrowValue, _PDF]
    page: Annotated[int | None, _PAGE]


_WORDS_COLUMNS_MD = (
    "| column | type | description |\n"
    "|---|---|---|\n"
    "| `page` | INTEGER | 1-based page number the word is on. |\n"
    "| `text` | VARCHAR | The word's text. |\n"
    "| `x0` | DOUBLE | Left edge, in PDF points from the left. |\n"
    "| `top` | DOUBLE | Top edge, in PDF points from the top. |\n"
    "| `x1` | DOUBLE | Right edge, in PDF points from the left. |\n"
    "| `bottom` | DOUBLE | Bottom edge, in PDF points from the top. |"
)

# VGI307/VGI414: structured result schema (replaces retired `vgi.result_columns_md`).
_WORDS_COLUMNS_SCHEMA = json.dumps(
    [
        {"name": "page", "type": "INTEGER", "description": "1-based page number the word is on."},
        {"name": "text", "type": "VARCHAR", "description": "The word's text."},
        {"name": "x0", "type": "DOUBLE", "description": "Left edge, in PDF points from the left."},
        {"name": "top", "type": "DOUBLE", "description": "Top edge, in PDF points from the top."},
        {"name": "x1", "type": "DOUBLE", "description": "Right edge, in PDF points from the left."},
        {"name": "bottom", "type": "DOUBLE", "description": "Bottom edge, in PDF points from the top."},
    ]
)

# VGI509 guaranteed-runnable examples (these are actually EXECUTED by the
# linter). Each `sql` is catalog-qualified and self-contained, driving the
# committed test fixtures by VARCHAR path relative to the worker's cwd. We omit
# `expected_result` deliberately -- the linter only needs each query to run.
_WORDS_EXECUTABLE_EXAMPLES = (
    "["
    '{"description": "Extract every word box from a small fixture PDF.",'
    ' "sql": "SELECT page, text, x0, top FROM pdf.main.words(pdf := \'test/sql/data/words.pdf\')'
    ' ORDER BY top, x0 LIMIT 5"},'
    '{"description": "Restrict word boxes to a single page.",'
    ' "sql": "SELECT count(*) AS n FROM pdf.main.words(pdf := \'test/sql/data/words.pdf\', page := 1)"},'
    '{"description": "List page geometry for a multi-page PDF.",'
    ' "sql": "SELECT page, width, height, rotation FROM pdf.main.pages(pdf := \'test/sql/data/multipage.pdf\')'
    ' ORDER BY page"},'
    '{"description": "Pull table cells in long format from a PDF that contains a table.",'
    ' "sql": "SELECT page, table_index, \\"row\\", col, value FROM pdf.main.tables(pdf := \'test/sql/data/table.pdf\')'
    ' ORDER BY page, table_index, \\"row\\", col LIMIT 8"}'
    "]"
)

_WORDS_TAGS = {
    **object_tags(
        title="Extract PDF Word Boxes",
        doc_llm=(
            "# words\n\n"
            "Table function returning **one row per word** in a PDF, with each word's text and its "
            "bounding box: `page` (1-based), `text`, and the box edges `x0`/`top`/`x1`/`bottom` in PDF "
            "points (origin at the top-left). Call by keyword: `words(pdf := '...')`, with the optional "
            "`page := N` filter to restrict to one page. `pdf` is a `VARCHAR` path or a `BLOB`.\n\n"
            "Use it to locate text by coordinate, reconstruct reading order (`ORDER BY top, x0`), find "
            "where a keyword appears on a page, or build a spatial layout index.\n\n"
            "**Edge cases:** a scanned/image-only page yields no words; a NULL `pdf` raises a clean "
            "argument error; an unreadable PDF raises a clean DuckDB error. Output streams in bounded "
            "slices, so pages with thousands of words page safely over every transport."
        ),
        doc_md=(
            "# Extract PDF Word Boxes\n\n"
            "Returns every word in a PDF together with its bounding box, one row per word.\n\n"
            "## Example\n\n"
            "Calling `pdf.main.words(pdf := 'invoice.pdf')` returns one row per word -- its `text` and "
            "the box edges `x0`, `top`, `x1`, `bottom` (PDF points, origin top-left). Order by `top` "
            "then `x0` to recover reading order, or pass `page := 1` to restrict to the first page. "
            "Ready-to-run SQL lives in the example queries.\n\n"
            "## Columns\n\n" + _WORDS_COLUMNS_MD + "\n\n"
            "## Notes\n\n"
            "Coordinates are in PDF points with the origin at the top-left. Image-only (scanned) pages "
            "produce no words -- this reads the text layer, it does not OCR. Call by keyword "
            "(`pdf := ...`, optional `page := N`)."
        ),
        keywords=[
            "words",
            "word boxes",
            "bounding box",
            "coordinates",
            "text position",
            "layout",
            "x0",
            "top",
            "reading order",
        ],
        category="Content",
        relative_path=_SRC,
    ),
    "vgi.result_columns_schema": _WORDS_COLUMNS_SCHEMA,
    "vgi.executable_examples": _WORDS_EXECUTABLE_EXAMPLES,
}


def _build_words(src: PdfSource, page: int | None, schema: pa.Schema) -> pa.RecordBatch:
    rows = core.words(src, page)
    return pa.RecordBatch.from_pydict(
        {
            "page": [r[0] for r in rows],
            "text": [r[1] for r in rows],
            "x0": [r[2] for r in rows],
            "top": [r[3] for r in rows],
            "x1": [r[4] for r in rows],
            "bottom": [r[5] for r in rows],
        },
        schema=schema,
    )


@init_single_worker
@bind_fixed_schema
class WordsFunction(TableFunctionGenerator[_WordsArgs, ScanState]):
    """``words(pdf[, page := ...])`` -- per-word bounding boxes of a PDF."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = _WORDS_SCHEMA

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "words"
        description = "Per-word bounding boxes (page, text, x0, top, x1, bottom) of a PDF (VARCHAR path or BLOB bytes)"
        categories = ["pdf", "words"]
        tags = _WORDS_TAGS
        examples = [
            FunctionExample(
                sql=(
                    "SELECT page, text, x0, top "
                    "FROM pdf.main.words(pdf := 'test/sql/data/words.pdf') ORDER BY top, x0 LIMIT 5"
                ),
                description="First few word boxes in reading order",
            ),
            FunctionExample(
                sql=(
                    "SELECT count(*) AS words_on_page_1 "
                    "FROM pdf.main.words(pdf := 'test/sql/data/words.pdf', page := 1)"
                ),
                description="Count the words on page 1",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[_WordsArgs]) -> TableCardinality:
        """Return the estimated output cardinality."""
        return TableCardinality(estimate=500, max=None)

    @classmethod
    def initial_state(cls, params: ProcessParams[_WordsArgs]) -> ScanState:
        """Return a fresh scan-state cursor for a new execution."""
        return ScanState()

    @classmethod
    def process(cls, params: ProcessParams[_WordsArgs], state: ScanState, out: OutputCollector) -> None:
        """Materialize the word batch once, then stream bounded slices."""
        if not state.started:
            src = _source_from_any(params.args.pdf)
            if src is None:
                out.finish()
                return
            state.rows_ipc = result_to_ipc(_build_words(src, params.args.page, params.output_schema))
            state.started = True
        _stream_slice(state, params.output_schema, out)


# ---------------------------------------------------------------------------
# tables(pdf, page := NULL) -> (page, table_index, row, col, value)
#
# ``row`` is a SQL keyword, so the column is quoted in SQL; the Arrow field
# name is "row" and the schema comment documents it.
# ---------------------------------------------------------------------------

_TABLES_SCHEMA = pa.schema(
    [
        field("page", pa.int32(), "1-based page number.", nullable=False),
        field("table_index", pa.int32(), "0-based table ordinal within the page.", nullable=False),
        field("row", pa.int32(), "0-based row index in the table (SQL keyword: quote it).", nullable=False),
        field("col", pa.int32(), "0-based column index within the table.", nullable=False),
        field("value", pa.string(), "Cell text (NULL for an empty/missing cell)."),
    ]
)


@dataclass(kw_only=True)
class _TablesArgs:
    pdf: Annotated[AnyArrowValue, _PDF]
    page: Annotated[int | None, _PAGE]


_TABLES_COLUMNS_MD = (
    "| column | type | description |\n"
    "|---|---|---|\n"
    "| `page` | INTEGER | 1-based page number the cell is on. |\n"
    "| `table_index` | INTEGER | 0-based table ordinal within the page. |\n"
    '| `row` | INTEGER | 0-based row index in the table (SQL keyword: quote as `"row"`). |\n'
    "| `col` | INTEGER | 0-based column index within the table. |\n"
    "| `value` | VARCHAR | Cell text (NULL for an empty/missing cell). |"
)

# VGI307/VGI414: structured result schema (replaces retired `vgi.result_columns_md`).
_TABLES_COLUMNS_SCHEMA = json.dumps(
    [
        {"name": "page", "type": "INTEGER", "description": "1-based page number the cell is on."},
        {"name": "table_index", "type": "INTEGER", "description": "0-based table ordinal within the page."},
        {
            "name": "row",
            "type": "INTEGER",
            "description": '0-based row index in the table (SQL keyword: quote as "row").',
        },
        {"name": "col", "type": "INTEGER", "description": "0-based column index within the table."},
        {"name": "value", "type": "VARCHAR", "description": "Cell text (NULL for an empty/missing cell)."},
    ]
)

_TABLES_TAGS = {
    **object_tags(
        title="Extract PDF Table Cells",
        doc_llm=(
            "# tables\n\n"
            "Table function that detects tables in a PDF and returns them in **long (tidy) format -- "
            "one row per cell**: `page` (1-based), `table_index` (0-based ordinal of the table on the "
            "page), `row` and `col` (0-based indices within the table), and `value` (the cell text, "
            "NULL when empty). Call by keyword: `tables(pdf := '...')`, with the optional `page := N` "
            "filter. `pdf` is a `VARCHAR` path or a `BLOB`.\n\n"
            "Use it to mine numeric/tabular data out of reports and statements. Pivot back to a grid "
            "with conditional aggregation on `row`/`col`, or filter a single table with "
            "`table_index`.\n\n"
            '**Gotcha:** `row` is a SQL keyword -- quote it as `"row"`. **Edge cases:** a page with no '
            "detectable table contributes no rows; a NULL `pdf` raises a clean argument error; an "
            "unreadable PDF raises a clean DuckDB error. Output streams in bounded slices."
        ),
        doc_md=(
            "# Extract PDF Table Cells\n\n"
            "Detects tables in a PDF and returns their cells in long (one-row-per-cell) format.\n\n"
            "## Example\n\n"
            "Calling `pdf.main.tables(pdf := 'report.pdf')` yields one row per cell, with `page`, "
            '`table_index`, `row`, `col`, and `value`. Order by `page, table_index, "row", col` to '
            "read cells in layout order, and pivot back to a grid with conditional aggregation over "
            "`row`/`col`. Ready-to-run SQL lives in the example queries.\n\n"
            "## Columns\n\n" + _TABLES_COLUMNS_MD + "\n\n"
            "## Notes\n\n"
            'The `row` column is a SQL keyword -- quote it as `"row"`. Pages without a detectable table '
            "produce no rows. Call by keyword (`pdf := ...`, optional `page := N`)."
        ),
        keywords=[
            "tables",
            "table cells",
            "extract table",
            "tabular",
            "cells",
            "rows",
            "columns",
            "long format",
            "grid",
            "spreadsheet",
        ],
        category="Content",
        relative_path=_SRC,
    ),
    "vgi.result_columns_schema": _TABLES_COLUMNS_SCHEMA,
}


def _build_tables(src: PdfSource, page: int | None, schema: pa.Schema) -> pa.RecordBatch:
    rows = core.tables(src, page)
    return pa.RecordBatch.from_pydict(
        {
            "page": [r[0] for r in rows],
            "table_index": [r[1] for r in rows],
            "row": [r[2] for r in rows],
            "col": [r[3] for r in rows],
            "value": [r[4] for r in rows],
        },
        schema=schema,
    )


@init_single_worker
@bind_fixed_schema
class TablesFunction(TableFunctionGenerator[_TablesArgs, ScanState]):
    """``tables(pdf[, page := ...])`` -- long-format table cells of a PDF."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = _TABLES_SCHEMA

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "tables"
        description = (
            "Long-format table cells (page, table_index, row, col, value) of a PDF (VARCHAR path or BLOB bytes)"
        )
        categories = ["pdf", "tables"]
        tags = _TABLES_TAGS
        examples = [
            FunctionExample(
                sql=(
                    'SELECT page, table_index, "row", col, value '
                    "FROM pdf.main.tables(pdf := 'test/sql/data/table.pdf') "
                    'ORDER BY page, table_index, "row", col LIMIT 8'
                ),
                description="Table cells in layout order (long format)",
            ),
            FunctionExample(
                sql=("SELECT count(*) AS cells FROM pdf.main.tables(pdf := 'test/sql/data/table.pdf', page := 1)"),
                description="Count the detected table cells on page 1",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[_TablesArgs]) -> TableCardinality:
        """Return the estimated output cardinality."""
        return TableCardinality(estimate=100, max=None)

    @classmethod
    def initial_state(cls, params: ProcessParams[_TablesArgs]) -> ScanState:
        """Return a fresh scan-state cursor for a new execution."""
        return ScanState()

    @classmethod
    def process(cls, params: ProcessParams[_TablesArgs], state: ScanState, out: OutputCollector) -> None:
        """Materialize the cell batch once, then stream bounded slices."""
        if not state.started:
            src = _source_from_any(params.args.pdf)
            if src is None:
                out.finish()
                return
            state.rows_ipc = result_to_ipc(_build_tables(src, params.args.page, params.output_schema))
            state.started = True
        _stream_slice(state, params.output_schema, out)


# ---------------------------------------------------------------------------
# functions -- a browsable discovery VIEW (name, kind, category, summary)
#
# VGI146 is only cleared by a real table or view (it checks ``iter_table_like``);
# a parameterless table function does NOT satisfy it (and VGI145/VGI311 flag a
# no-arg table function or a function-wrapping view). So the worker's browsable
# entry point is a VALUES-backed ``CatalogView``: it is a genuine view, scans
# with no file/credential/network (clearing VGI911 for free), and gives an agent
# a place to see what the worker offers before it has to guess any argument.
# ---------------------------------------------------------------------------

# The registry rows. Kept in step with the objects actually registered in
# ``pdf_worker.py``; the row order groups by capability for easy browsing.
_FUNCTIONS_ROWS: list[tuple[str, str, str, str]] = [
    ("page_count", "scalar", "Structure", "Number of pages in a PDF."),
    ("pages", "table function", "Structure", "One row per page with width, height, and rotation."),
    ("words", "table function", "Content", "One row per word with its bounding box."),
    ("tables", "table function", "Content", "Detected table cells in long (one-row-per-cell) format."),
    ("form_fields", "scalar", "Content", "AcroForm field name to value, as a MAP."),
    ("pdf_metadata", "scalar", "Metadata", "Document information dictionary (Title/Author/...), as a MAP."),
    ("is_encrypted", "scalar", "Metadata", "Whether a PDF is encrypted."),
    ("render_page", "scalar", "Rendering", "Render one page to a PNG image (BLOB)."),
    ("functions", "view", "Discovery", "This registry: a browsable view of every object the worker exposes."),
]


def _sql_literal(value: str) -> str:
    """Render ``value`` as a single-quoted SQL string literal (quotes doubled)."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


# A static VALUES scan is the whole view -- no PDF, file, or network is touched,
# so a bare ``SELECT ... LIMIT 10`` probe (VGI911) answers instantly.
_FUNCTIONS_VIEW_DEFINITION = (
    "SELECT name, kind, category, summary FROM (VALUES\n    "
    + ",\n    ".join("(" + ", ".join(_sql_literal(cell) for cell in row) + ")" for row in _FUNCTIONS_ROWS)
    + "\n) AS t(name, kind, category, summary)"
)

_FUNCTIONS_COLUMN_COMMENTS = {
    "name": "Object name (call it as pdf.main.<name>).",
    "kind": "'scalar', 'table function', or 'view'.",
    "category": "Category grouping: Structure, Content, Metadata, Rendering, or Discovery.",
    "summary": "One-line description of what the object returns.",
}

_FUNCTIONS_COLUMNS_SCHEMA = json.dumps(
    [
        {"name": "name", "type": "VARCHAR", "description": _FUNCTIONS_COLUMN_COMMENTS["name"]},
        {"name": "kind", "type": "VARCHAR", "description": _FUNCTIONS_COLUMN_COMMENTS["kind"]},
        {"name": "category", "type": "VARCHAR", "description": _FUNCTIONS_COLUMN_COMMENTS["category"]},
        {"name": "summary", "type": "VARCHAR", "description": _FUNCTIONS_COLUMN_COMMENTS["summary"]},
    ]
)

# VGI502 object-level example queries: a JSON array of {description, sql}. Fully
# catalog-qualified and column-projecting (never a bare SELECT *), so they count
# toward this object's example coverage and run cleanly under --execute.
_FUNCTIONS_EXAMPLE_QUERIES = json.dumps(
    [
        {
            "description": "Browse every object the worker exposes, grouped by category",
            "sql": "SELECT name, kind, category FROM pdf.main.functions ORDER BY category, name",
        },
        {
            "description": "List just the content-extraction objects",
            "sql": ("SELECT name, summary FROM pdf.main.functions WHERE category = 'Content' ORDER BY name"),
        },
        {
            "description": "Count how many objects the worker exposes",
            "sql": "SELECT count(*) AS object_count FROM pdf.main.functions",
        },
    ]
)

_FUNCTIONS_TAGS = {
    **object_tags(
        title="List Worker Objects",
        doc_llm=(
            "# functions\n\n"
            "A **browsable discovery view** listing every object this worker exposes, one row per "
            "object: `name`, `kind` (`scalar`, `table function`, or `view`), `category` "
            "(Structure/Content/Metadata/Rendering/Discovery), and a one-line `summary`. It is a plain "
            "view -- read it as `pdf.main.functions` with no arguments.\n\n"
            "Use it as the entry point when you do not yet know what the worker offers: read this view "
            "first to pick the right object, then call that object with a PDF path or bytes. Filter by "
            "`category` to narrow to a capability area, or by `kind` to separate per-row scalars from "
            "set-returning table functions."
        ),
        doc_md=(
            "# List Worker Objects\n\n"
            "A static registry view of every object this worker exposes -- the browsable entry point for "
            "discovering the surface before calling anything.\n\n"
            "## Example\n\n"
            "Reading `pdf.main.functions` returns one row per object with its `name`, `kind`, "
            "`category`, and `summary`; keep rows whose `category` equals `Content` to see the "
            "table-cell, word-box, and form-field extractors. Ready-to-run SQL lives in the example "
            "queries.\n\n"
            "## Columns\n\n"
            "| column | type | description |\n"
            "|---|---|---|\n"
            "| `name` | VARCHAR | Object name (call it as `pdf.main.<name>`). |\n"
            "| `kind` | VARCHAR | `scalar`, `table function`, or `view`. |\n"
            "| `category` | VARCHAR | Structure, Content, Metadata, Rendering, or Discovery. |\n"
            "| `summary` | VARCHAR | One-line description of what the object returns. |\n\n"
            "## Notes\n\n"
            "The view is static and offline -- it needs no PDF, file, or network access, so it always "
            "answers instantly."
        ),
        keywords=[
            "functions",
            "discovery",
            "registry",
            "capabilities",
            "catalog",
            "list functions",
            "browse",
            "help",
        ],
        category="Discovery",
        relative_path=_SRC,
    ),
    "vgi.result_columns_schema": _FUNCTIONS_COLUMNS_SCHEMA,
    "vgi.example_queries": _FUNCTIONS_EXAMPLE_QUERIES,
    # VGI123 classifying tags use BARE keys (not vgi.-namespaced) for faceting;
    # mirror the schema's values so the view groups with the rest of the worker.
    "domain": "documents",
    "category": "discovery",
    "topic": "pdf-structure-extraction",
}

FUNCTIONS_VIEW = View(
    name="functions",
    definition=_FUNCTIONS_VIEW_DEFINITION,
    comment="Registry of every object this worker exposes (name, kind, category, summary).",
    column_comments=dict(_FUNCTIONS_COLUMN_COMMENTS),
    tags=_FUNCTIONS_TAGS,
)


TABLE_FUNCTIONS: list[type] = [
    TablesFunction,
    WordsFunction,
    PagesFunction,
]
