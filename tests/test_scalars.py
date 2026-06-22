"""End-to-end tests for the per-row scalar pdf functions.

These spawn ``pdf_worker.py`` as a subprocess via ``vgi.client.Client`` and call
each scalar exactly as DuckDB would after ``ATTACH``, exercising the polymorphic
``pdf`` input (a VARCHAR path or a BLOB of bytes) and the ``render_page`` dpi
overload. This is the authoritative wire-level check that complements the SQL
suite.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pytest
from vgi import Arguments
from vgi.client import Client

from . import fixtures as fx

_WORKER = str(Path(__file__).resolve().parent.parent / "pdf_worker.py")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(scope="module")
def client() -> Iterator[Client]:
    # worker_limit=1 so output order matches input order for deterministic
    # per-row assertions.
    with Client(f"{sys.executable} {_WORKER}", worker_limit=1) as c:
        yield c


def _scalar_bytes(
    client: Client,
    name: str,
    blobs: list[bytes | None],
    *,
    extra_cols: dict[str, pa.Array] | None = None,
    positional: list[pa.Scalar] | None = None,
) -> list:
    cols = {"pdf": pa.array(blobs, type=pa.binary())}
    if extra_cols:
        cols.update(extra_cols)
    batch = pa.RecordBatch.from_pydict(cols)
    results = list(
        client.scalar_function(
            function_name=name,
            input=iter([batch]),
            arguments=Arguments(positional=positional or []),
        )
    )
    return results[0]["result"].to_pylist()


class TestPageCount:
    def test_bytes(self, client: Client) -> None:
        out = _scalar_bytes(
            client,
            "page_count",
            [fx.make_words_pdf(), fx.make_multipage_pdf(), None, fx.make_garbage_bytes()],
        )
        assert out == [1, 3, None, None]


class TestIsEncrypted:
    def test_bytes(self, client: Client) -> None:
        out = _scalar_bytes(
            client,
            "is_encrypted",
            [fx.make_words_pdf(), fx.make_encrypted_pdf(), None, fx.make_garbage_bytes()],
        )
        assert out == [False, True, None, None]


class TestMetadata:
    def test_title(self, client: Client) -> None:
        out = _scalar_bytes(client, "pdf_metadata", [fx.make_meta_pdf()])
        meta = dict(out[0])
        assert meta["Title"] == fx.KNOWN_TITLE

    def test_null_and_garbage(self, client: Client) -> None:
        out = _scalar_bytes(client, "pdf_metadata", [None, fx.make_garbage_bytes()])
        assert out == [None, None]


class TestFormFields:
    def test_value(self, client: Client) -> None:
        out = _scalar_bytes(client, "form_fields", [fx.make_form_pdf()])
        fields = dict(out[0])
        assert fields[fx.FORM_FIELD_NAME] == fx.FORM_FIELD_VALUE


class TestRenderPage:
    def test_default_dpi(self, client: Client) -> None:
        out = _scalar_bytes(
            client,
            "render_page",
            [fx.make_words_pdf(), None],
            extra_cols={"page": pa.array([1, 1], type=pa.int32())},
        )
        assert out[0][:8] == PNG_MAGIC
        assert out[1] is None

    def test_explicit_dpi(self, client: Client) -> None:
        out = _scalar_bytes(
            client,
            "render_page",
            [fx.make_words_pdf()],
            extra_cols={"page": pa.array([1], type=pa.int32())},
            positional=[pa.scalar(72, type=pa.int32())],
        )
        assert out[0][:8] == PNG_MAGIC

    def test_out_of_range_page_null(self, client: Client) -> None:
        out = _scalar_bytes(
            client,
            "render_page",
            [fx.make_words_pdf()],
            extra_cols={"page": pa.array([99], type=pa.int32())},
        )
        assert out[0] is None


class TestPathInput:
    """The VARCHAR-path overload, over a committed fixture on disk."""

    def test_page_count_path(self, client: Client) -> None:
        path = str(Path(__file__).resolve().parent.parent / "test" / "sql" / "data" / "multipage.pdf")
        batch = pa.RecordBatch.from_pydict({"pdf": pa.array([path], type=pa.string())})
        results = list(
            client.scalar_function(
                function_name="page_count",
                input=iter([batch]),
                arguments=Arguments(positional=[]),
            )
        )
        assert results[0]["result"].to_pylist() == [3]
