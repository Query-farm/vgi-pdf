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

from dataclasses import dataclass
from typing import Annotated, ClassVar

import pyarrow as pa
from vgi.arguments import AnyArrow, AnyArrowValue, Arg
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
from .schema_utils import field

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
    doc="The PDF as a VARCHAR filesystem path the worker opens, or a BLOB of the raw PDF bytes.",
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
        tags = {"vgi.columns_md": _PAGES_COLUMNS_MD}
        examples = [
            FunctionExample(
                sql="SELECT * FROM pdf.pages(pdf := 'doc.pdf')",
                description="Page geometry of a PDF file",
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
        tags = {"vgi.columns_md": _WORDS_COLUMNS_MD}
        examples = [
            FunctionExample(
                sql="SELECT * FROM pdf.words(pdf := 'doc.pdf') ORDER BY page, top, x0",
                description="All word boxes in reading order",
            ),
            FunctionExample(
                sql="SELECT * FROM pdf.words(pdf := 'doc.pdf', page := 1)",
                description="Word boxes on page 1 only",
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
        tags = {"vgi.columns_md": _TABLES_COLUMNS_MD}
        examples = [
            FunctionExample(
                sql="SELECT * FROM pdf.tables(pdf := 'report.pdf') ORDER BY page, table_index, row, col",
                description="Every table cell as a tidy row",
            ),
            FunctionExample(
                sql="SELECT * FROM pdf.tables(pdf := 'report.pdf', page := 1)",
                description="Table cells on page 1 only",
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


TABLE_FUNCTIONS: list[type] = [
    TablesFunction,
    WordsFunction,
    PagesFunction,
]
