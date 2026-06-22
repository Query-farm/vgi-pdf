"""In-process tests for the pdf table functions (tables, words, pages).

Drives each table function through the real bind -> init -> process lifecycle
in-process (no worker subprocess), passing the polymorphic ``pdf`` argument as
BLOB bytes and exercising the optional ``page`` named filter.
"""

from __future__ import annotations

import pyarrow as pa

from vgi_pdf.tables import (
    PagesBytesFunction,
    TablesBytesFunction,
    WordsBytesFunction,
)

from . import fixtures as fx
from .harness import invoke_table_function


def _blob(data: bytes) -> pa.Scalar:
    return pa.scalar(data, type=pa.binary())


class TestTables:
    def test_known_2x2(self) -> None:
        table = invoke_table_function(TablesBytesFunction, positional=(_blob(fx.make_table_pdf()),))
        assert table.column_names == ["page", "table_index", "row", "col", "value"]
        rows = {(r["page"], r["table_index"], r["row"], r["col"]): r["value"] for r in table.to_pylist()}
        assert rows[(1, 0, 0, 0)] == "Name"
        assert rows[(1, 0, 0, 1)] == "Age"
        assert rows[(1, 0, 1, 0)] == "Ada"
        assert rows[(1, 0, 1, 1)] == "36"
        assert table.num_rows == 4

    def test_page_filter(self) -> None:
        table = invoke_table_function(
            TablesBytesFunction,
            positional=(_blob(fx.make_table_pdf()),),
            named={"page": pa.scalar(2, type=pa.int32())},
        )
        assert table.num_rows == 0  # only one page

    def test_null_pdf_no_rows(self) -> None:
        table = invoke_table_function(TablesBytesFunction, positional=(pa.scalar(None, type=pa.binary()),))
        assert table.num_rows == 0


class TestWords:
    def test_known_words(self) -> None:
        table = invoke_table_function(WordsBytesFunction, positional=(_blob(fx.make_words_pdf()),))
        assert table.column_names == ["page", "text", "x0", "top", "x1", "bottom"]
        texts = table.column("text").to_pylist()
        assert texts == ["Hello", "World", "Structured", "Layout"]

    def test_boxes_valid(self) -> None:
        table = invoke_table_function(WordsBytesFunction, positional=(_blob(fx.make_words_pdf()),))
        for r in table.to_pylist():
            assert r["x0"] < r["x1"]
            assert r["top"] < r["bottom"]


class TestPages:
    def test_geometry(self) -> None:
        table = invoke_table_function(PagesBytesFunction, positional=(_blob(fx.make_multipage_pdf()),))
        assert table.column_names == ["page", "width", "height", "rotation"]
        assert table.column("page").to_pylist() == [1, 2, 3]
        assert table.column("width").to_pylist() == [612.0, 612.0, 612.0]
        assert table.column("height").to_pylist() == [792.0, 792.0, 792.0]
