"""Set-returning PDF structure table functions for DuckDB.

These expand to **many rows** per PDF, so they are exposed as **table
functions** -- the form that accepts DuckDB ``name := value`` arguments
(``page``). The per-row, single-value PDF functions are *scalars* and live in
:mod:`vgi_pdf.scalars`.

    SELECT * FROM pdf.tables('report.pdf');                 -- every cell, long format
    SELECT * FROM pdf.tables('report.pdf', page := 1);      -- only page 1
    SELECT * FROM pdf.words('report.pdf') ORDER BY top, x0; -- word boxes
    SELECT * FROM pdf.pages('report.pdf');                  -- page geometry

Polymorphic ``pdf`` input
-------------------------
The first positional argument is **either** a ``VARCHAR`` filesystem path the
worker opens **or** a ``BLOB`` of raw PDF bytes. DuckDB dispatches on the
argument type, so each table function is registered twice -- a ``*PathFunction``
(``Arg`` typed ``pa.string()``) and a ``*BytesFunction`` (typed ``pa.binary()``)
-- sharing one ``Meta.name``.

Hostile input: an unreadable / encrypted / malformed PDF surfaces a clean
DuckDB error (raised from :mod:`vgi_pdf.core`), never a worker crash or hang. A
NULL ``pdf`` argument yields **no rows**.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
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
class _PagesPathArgs:
    pdf: Annotated[str | None, Arg(0, arrow_type=pa.string(), doc="Filesystem path to a PDF.")]


@dataclass(kw_only=True)
class _PagesBytesArgs:
    pdf: Annotated[bytes | None, Arg(0, arrow_type=pa.binary(), doc="Raw PDF bytes.")]


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
class PagesPathFunction(TableFunctionGenerator[_PagesPathArgs, ScanState]):
    """``pages(path)`` -- per-page geometry of a PDF at a filesystem path."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = _PAGES_SCHEMA

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "pages"
        description = "Per-page geometry (page, width, height, rotation) of a PDF (VARCHAR path)"
        categories = ["pdf", "structure"]
        examples = [
            FunctionExample(
                sql="SELECT * FROM pdf.pages('doc.pdf')",
                description="Page geometry of a PDF file",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[_PagesPathArgs]) -> TableCardinality:
        """Return the estimated output cardinality."""
        return TableCardinality(estimate=10, max=None)

    @classmethod
    def initial_state(cls, params: ProcessParams[_PagesPathArgs]) -> ScanState:
        """Return a fresh scan-state cursor for a new execution."""
        return ScanState()

    @classmethod
    def process(cls, params: ProcessParams[_PagesPathArgs], state: ScanState, out: OutputCollector) -> None:
        """Materialize the page batch once, then stream bounded slices."""
        if not state.started:
            src = PdfSource.from_path(params.args.pdf)
            if src is None:
                out.finish()
                return
            state.rows_ipc = result_to_ipc(_build_pages(src, params.output_schema))
            state.started = True
        _stream_slice(state, params.output_schema, out)


@init_single_worker
@bind_fixed_schema
class PagesBytesFunction(TableFunctionGenerator[_PagesBytesArgs, ScanState]):
    """``pages(blob)`` -- per-page geometry of a PDF passed as bytes."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = _PAGES_SCHEMA

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "pages"
        description = "Per-page geometry (page, width, height, rotation) of a PDF (BLOB bytes)"
        categories = ["pdf", "structure"]
        examples = [
            FunctionExample(
                sql="SELECT * FROM pdf.pages(blob)",
                description="Page geometry of a PDF held as bytes",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[_PagesBytesArgs]) -> TableCardinality:
        """Return the estimated output cardinality."""
        return TableCardinality(estimate=10, max=None)

    @classmethod
    def initial_state(cls, params: ProcessParams[_PagesBytesArgs]) -> ScanState:
        """Return a fresh scan-state cursor for a new execution."""
        return ScanState()

    @classmethod
    def process(cls, params: ProcessParams[_PagesBytesArgs], state: ScanState, out: OutputCollector) -> None:
        """Materialize the page batch once, then stream bounded slices."""
        if not state.started:
            src = PdfSource.from_bytes(params.args.pdf)
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
class _WordsPathArgs:
    pdf: Annotated[str | None, Arg(0, arrow_type=pa.string(), doc="Filesystem path to a PDF.")]
    page: Annotated[int | None, _PAGE]


@dataclass(kw_only=True)
class _WordsBytesArgs:
    pdf: Annotated[bytes | None, Arg(0, arrow_type=pa.binary(), doc="Raw PDF bytes.")]
    page: Annotated[int | None, _PAGE]


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
class WordsPathFunction(TableFunctionGenerator[_WordsPathArgs, ScanState]):
    """``words(path[, page := ...])`` -- per-word boxes for a PDF at a path."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = _WORDS_SCHEMA

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "words"
        description = "Per-word bounding boxes (page, text, x0, top, x1, bottom) of a PDF (VARCHAR path)"
        categories = ["pdf", "words"]
        examples = [
            FunctionExample(
                sql="SELECT * FROM pdf.words('doc.pdf') ORDER BY page, top, x0",
                description="All word boxes in reading order",
            ),
            FunctionExample(
                sql="SELECT * FROM pdf.words('doc.pdf', page := 1)",
                description="Word boxes on page 1 only",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[_WordsPathArgs]) -> TableCardinality:
        """Return the estimated output cardinality."""
        return TableCardinality(estimate=500, max=None)

    @classmethod
    def initial_state(cls, params: ProcessParams[_WordsPathArgs]) -> ScanState:
        """Return a fresh scan-state cursor for a new execution."""
        return ScanState()

    @classmethod
    def process(cls, params: ProcessParams[_WordsPathArgs], state: ScanState, out: OutputCollector) -> None:
        """Materialize the word batch once, then stream bounded slices."""
        if not state.started:
            src = PdfSource.from_path(params.args.pdf)
            if src is None:
                out.finish()
                return
            state.rows_ipc = result_to_ipc(_build_words(src, params.args.page, params.output_schema))
            state.started = True
        _stream_slice(state, params.output_schema, out)


@init_single_worker
@bind_fixed_schema
class WordsBytesFunction(TableFunctionGenerator[_WordsBytesArgs, ScanState]):
    """``words(blob[, page := ...])`` -- per-word boxes for a PDF as bytes."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = _WORDS_SCHEMA

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "words"
        description = "Per-word bounding boxes (page, text, x0, top, x1, bottom) of a PDF (BLOB bytes)"
        categories = ["pdf", "words"]
        examples = [
            FunctionExample(
                sql="SELECT * FROM pdf.words(blob) ORDER BY page, top, x0",
                description="All word boxes from PDF bytes",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[_WordsBytesArgs]) -> TableCardinality:
        """Return the estimated output cardinality."""
        return TableCardinality(estimate=500, max=None)

    @classmethod
    def initial_state(cls, params: ProcessParams[_WordsBytesArgs]) -> ScanState:
        """Return a fresh scan-state cursor for a new execution."""
        return ScanState()

    @classmethod
    def process(cls, params: ProcessParams[_WordsBytesArgs], state: ScanState, out: OutputCollector) -> None:
        """Materialize the word batch once, then stream bounded slices."""
        if not state.started:
            src = PdfSource.from_bytes(params.args.pdf)
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
class _TablesPathArgs:
    pdf: Annotated[str | None, Arg(0, arrow_type=pa.string(), doc="Filesystem path to a PDF.")]
    page: Annotated[int | None, _PAGE]


@dataclass(kw_only=True)
class _TablesBytesArgs:
    pdf: Annotated[bytes | None, Arg(0, arrow_type=pa.binary(), doc="Raw PDF bytes.")]
    page: Annotated[int | None, _PAGE]


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
class TablesPathFunction(TableFunctionGenerator[_TablesPathArgs, ScanState]):
    """``tables(path[, page := ...])`` -- long-format table cells (PDF at a path)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = _TABLES_SCHEMA

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "tables"
        description = "Long-format table cells (page, table_index, row, col, value) of a PDF (VARCHAR path)"
        categories = ["pdf", "tables"]
        examples = [
            FunctionExample(
                sql="SELECT * FROM pdf.tables('report.pdf') ORDER BY page, table_index, row, col",
                description="Every table cell as a tidy row",
            ),
            FunctionExample(
                sql="SELECT * FROM pdf.tables('report.pdf', page := 1)",
                description="Table cells on page 1 only",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[_TablesPathArgs]) -> TableCardinality:
        """Return the estimated output cardinality."""
        return TableCardinality(estimate=100, max=None)

    @classmethod
    def initial_state(cls, params: ProcessParams[_TablesPathArgs]) -> ScanState:
        """Return a fresh scan-state cursor for a new execution."""
        return ScanState()

    @classmethod
    def process(cls, params: ProcessParams[_TablesPathArgs], state: ScanState, out: OutputCollector) -> None:
        """Materialize the cell batch once, then stream bounded slices."""
        if not state.started:
            src = PdfSource.from_path(params.args.pdf)
            if src is None:
                out.finish()
                return
            state.rows_ipc = result_to_ipc(_build_tables(src, params.args.page, params.output_schema))
            state.started = True
        _stream_slice(state, params.output_schema, out)


@init_single_worker
@bind_fixed_schema
class TablesBytesFunction(TableFunctionGenerator[_TablesBytesArgs, ScanState]):
    """``tables(blob[, page := ...])`` -- long-format table cells (PDF as bytes)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = _TABLES_SCHEMA

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "tables"
        description = "Long-format table cells (page, table_index, row, col, value) of a PDF (BLOB bytes)"
        categories = ["pdf", "tables"]
        examples = [
            FunctionExample(
                sql="SELECT * FROM pdf.tables(blob) ORDER BY page, table_index, row, col",
                description="Every table cell from PDF bytes",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[_TablesBytesArgs]) -> TableCardinality:
        """Return the estimated output cardinality."""
        return TableCardinality(estimate=100, max=None)

    @classmethod
    def initial_state(cls, params: ProcessParams[_TablesBytesArgs]) -> ScanState:
        """Return a fresh scan-state cursor for a new execution."""
        return ScanState()

    @classmethod
    def process(cls, params: ProcessParams[_TablesBytesArgs], state: ScanState, out: OutputCollector) -> None:
        """Materialize the cell batch once, then stream bounded slices."""
        if not state.started:
            src = PdfSource.from_bytes(params.args.pdf)
            if src is None:
                out.finish()
                return
            state.rows_ipc = result_to_ipc(_build_tables(src, params.args.page, params.output_schema))
            state.started = True
        _stream_slice(state, params.output_schema, out)


TABLE_FUNCTIONS: list[type] = [
    TablesPathFunction,
    TablesBytesFunction,
    WordsPathFunction,
    WordsBytesFunction,
    PagesPathFunction,
    PagesBytesFunction,
]
