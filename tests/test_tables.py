"""In-process tests for the pdf table functions (tables, words, pages).

Drives each table function through the real bind -> init -> process lifecycle
in-process (no worker subprocess), passing the polymorphic ``pdf`` argument as
BLOB bytes (via the named ``pdf`` parameter) and exercising the optional
``page`` named filter.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from vgi_pdf import tables as tables_mod
from vgi_pdf.tables import (
    PagesFunction,
    TablesFunction,
    WordsFunction,
)

from . import fixtures as fx
from .harness import invoke_table_function


def _blob(data: bytes) -> pa.Scalar:
    return pa.scalar(data, type=pa.binary())


class TestTables:
    def test_known_2x2(self) -> None:
        table = invoke_table_function(TablesFunction, named={"pdf": _blob(fx.make_table_pdf())})
        assert table.column_names == ["page", "table_index", "row", "col", "value"]
        rows = {(r["page"], r["table_index"], r["row"], r["col"]): r["value"] for r in table.to_pylist()}
        assert rows[(1, 0, 0, 0)] == "Name"
        assert rows[(1, 0, 0, 1)] == "Age"
        assert rows[(1, 0, 1, 0)] == "Ada"
        assert rows[(1, 0, 1, 1)] == "36"
        assert table.num_rows == 4

    def test_page_filter(self) -> None:
        table = invoke_table_function(
            TablesFunction,
            named={"pdf": _blob(fx.make_table_pdf()), "page": pa.scalar(2, type=pa.int32())},
        )
        assert table.num_rows == 0  # only one page

    def test_null_pdf_rejected(self) -> None:
        # The single ANY-typed ``pdf`` parameter is required and non-nullable, so
        # a NULL ``pdf`` surfaces a clean validation error (not a crash, not rows).
        from vgi.arguments import ArgumentValidationError

        with pytest.raises(ArgumentValidationError):
            invoke_table_function(TablesFunction, named={"pdf": pa.scalar(None, type=pa.binary())})


class TestWords:
    def test_known_words(self) -> None:
        table = invoke_table_function(WordsFunction, named={"pdf": _blob(fx.make_words_pdf())})
        assert table.column_names == ["page", "text", "x0", "top", "x1", "bottom"]
        texts = table.column("text").to_pylist()
        assert texts == ["Hello", "World", "Structured", "Layout"]

    def test_boxes_valid(self) -> None:
        table = invoke_table_function(WordsFunction, named={"pdf": _blob(fx.make_words_pdf())})
        for r in table.to_pylist():
            assert r["x0"] < r["x1"]
            assert r["top"] < r["bottom"]


class TestPages:
    def test_geometry(self) -> None:
        table = invoke_table_function(PagesFunction, named={"pdf": _blob(fx.make_multipage_pdf())})
        assert table.column_names == ["page", "width", "height", "rotation"]
        assert table.column("page").to_pylist() == [1, 2, 3]
        assert table.column("width").to_pylist() == [612.0, 612.0, 612.0]
        assert table.column("height").to_pylist() == [792.0, 792.0, 792.0]


class TestScanStateRoundTrip:
    """The HTTP-continuation regression guard.

    A ``words`` result that exceeds ``ROWS_PER_TICK`` must page across the
    stateless-transport limit-1 continuation boundary. Re-serializing the scan
    state between every ``process`` tick reproduces what the HTTP transport does
    on the wire. On the old emit-all + ``state: None`` code this loops forever
    (re-reading the PDF and re-emitting row 0 each resume) and trips the harness
    1000-tick guard; on the cursor code it terminates with identical rows.
    """

    def test_words_identical_with_and_without_serialization(self) -> None:
        # > ROWS_PER_TICK (64) words so the scan spans several ticks.
        pdf = fx.make_many_words_pdf(200)
        plain = invoke_table_function(WordsFunction, named={"pdf": _blob(pdf)})
        rt = invoke_table_function(
            WordsFunction,
            named={"pdf": _blob(pdf)},
            serialize_state=True,
        )
        assert plain.num_rows > tables_mod.ROWS_PER_TICK
        # Identical rows, identical order -- no dupes, no drops, full termination.
        assert rt.num_rows == plain.num_rows
        assert rt.to_pylist() == plain.to_pylist()
        # Each emitted word appears exactly once.
        texts = rt.column("text").to_pylist()
        assert len(texts) == len(set(texts))

    def test_words_chunks_bounded(self) -> None:
        # Drive the lifecycle directly to inspect per-tick emit sizes: no batch
        # may exceed ROWS_PER_TICK, and every word survives exactly once.
        from vgi.arguments import Arguments
        from vgi.function_storage import BoundStorage, FunctionStorageSqlite
        from vgi.invocation import FunctionType
        from vgi.protocol import BindRequest, InitRequest
        from vgi.table_function import ProcessParams

        from .harness import MockOutputCollector

        func = WordsFunction
        pdf = fx.make_many_words_pdf(200)
        args = Arguments(positional=(), named={"pdf": _blob(pdf)})
        bind_req = BindRequest(function_name=func.Meta.name, arguments=args, function_type=FunctionType.TABLE)
        bind_resp = func.bind(bind_req)
        init_req = InitRequest(bind_call=bind_req, output_schema=bind_resp.output_schema)
        init_resp = func.global_init(init_req)
        params = ProcessParams(
            args=func._parse_arguments(func.FunctionArguments, args),
            init_call=init_req,
            init_response=init_resp,
            output_schema=bind_resp.output_schema,
            settings={},
            secrets={},
            storage=BoundStorage(FunctionStorageSqlite(":memory:"), init_resp.execution_id),
        )
        state = func.initial_state(params)
        out = MockOutputCollector(bind_resp.output_schema)
        while not out.finished:
            func.process(params, state, out)
            state = type(state).deserialize_from_bytes(state.serialize_to_bytes())
        assert len(out.batches) >= 2  # genuinely paged
        for b in out.batches:
            assert b.num_rows <= tables_mod.ROWS_PER_TICK


class TestCursorSurvivesContinuation:
    """``tables`` (one row per cell) also pages across the continuation boundary."""

    def test_tables_identical_with_serialization(self) -> None:
        pdf = fx.make_table_pdf()
        plain = invoke_table_function(TablesFunction, named={"pdf": _blob(pdf)})
        rt = invoke_table_function(TablesFunction, named={"pdf": _blob(pdf)}, serialize_state=True)
        assert rt.to_pylist() == plain.to_pylist()

    def test_small_chunk_spans_batches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force ROWS_PER_TICK tiny so the 4-cell table genuinely spans batches,
        # then prove the cursor round-trips correctly across each one.
        monkeypatch.setattr(tables_mod, "ROWS_PER_TICK", 2)
        pdf = fx.make_table_pdf()
        plain = invoke_table_function(TablesFunction, named={"pdf": _blob(pdf)})
        rt = invoke_table_function(TablesFunction, named={"pdf": _blob(pdf)}, serialize_state=True)
        assert plain.num_rows == 4
        assert rt.num_rows == 4
        assert rt.to_pylist() == plain.to_pylist()

    def test_empty_result_terminates(self) -> None:
        # Page filter selecting a non-existent page -> 0 rows; must still finish.
        rt = invoke_table_function(
            TablesFunction,
            named={"pdf": _blob(fx.make_table_pdf()), "page": pa.scalar(2, type=pa.int32())},
            serialize_state=True,
        )
        assert rt.num_rows == 0
