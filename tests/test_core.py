"""Unit tests for the pure ``vgi_pdf.core`` PDF logic.

These call the pure functions directly (no Arrow, no VGI) over PDFs generated
deterministically in-test. They cover the happy paths AND the hostile-input
contract: garbage/empty bytes -> None / clean error, encrypted PDFs handled
gracefully, render_page bounds + PNG magic bytes.
"""

from __future__ import annotations

import pytest

from vgi_pdf import core
from vgi_pdf.core import PdfError, PdfSource

from . import fixtures as fx

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _bytes(data: bytes) -> PdfSource:
    src = PdfSource.from_bytes(data)
    assert src is not None
    return src


# --------------------------------------------------------------------------- #
# page_count / pages
# --------------------------------------------------------------------------- #


def test_page_count_single() -> None:
    assert core.page_count(_bytes(fx.make_words_pdf())) == 1


def test_page_count_multipage() -> None:
    assert core.page_count(_bytes(fx.make_multipage_pdf())) == 3


def test_pages_geometry() -> None:
    rows = core.pages(_bytes(fx.make_words_pdf()))
    assert rows == [(1, 612.0, 792.0, 0)]


def test_pages_multipage() -> None:
    rows = core.pages(_bytes(fx.make_multipage_pdf()))
    assert [r[0] for r in rows] == [1, 2, 3]
    assert all(r[1:] == (612.0, 792.0, 0) for r in rows)


# --------------------------------------------------------------------------- #
# tables — the headline: a known 2x2 table.
# --------------------------------------------------------------------------- #


def test_tables_known_2x2() -> None:
    rows = core.tables(_bytes(fx.make_table_pdf()))
    # long format: page, table_index, row, col, value
    assert (1, 0, 0, 0, "Name") in rows
    assert (1, 0, 0, 1, "Age") in rows
    assert (1, 0, 1, 0, "Ada") in rows
    assert (1, 0, 1, 1, "36") in rows
    # exactly four cells in one 2x2 table.
    assert len(rows) == 4
    assert {r[1] for r in rows} == {0}


def test_tables_page_filter() -> None:
    src = _bytes(fx.make_table_pdf())
    assert core.tables(src, page=1) == core.tables(src)
    assert core.tables(src, page=2) == []  # only one page


# --------------------------------------------------------------------------- #
# words — known text + box ordering.
# --------------------------------------------------------------------------- #


def test_words_known_text() -> None:
    rows = core.words(_bytes(fx.make_words_pdf()))
    texts = [r[1] for r in rows]
    assert texts == ["Hello", "World", "Structured", "Layout"]


def test_words_boxes_ordered() -> None:
    rows = core.words(_bytes(fx.make_words_pdf()))
    # Each box is (x0, top, x1, bottom) with x0 < x1 and top < bottom.
    for _page, _text, x0, top, x1, bottom in rows:
        assert x0 < x1
        assert top < bottom
    # "Hello" is left of "World" on the same line (same top).
    hello = next(r for r in rows if r[1] == "Hello")
    world = next(r for r in rows if r[1] == "World")
    assert hello[3] == world[3]  # same top
    assert hello[2] <= world[2]  # hello.x1 <= world.x0-ish (left of)
    # "Structured" is on a lower line than "Hello" (greater top).
    structured = next(r for r in rows if r[1] == "Structured")
    assert structured[3] > hello[3]


def test_words_page_filter() -> None:
    src = _bytes(fx.make_multipage_pdf())
    all_rows = core.words(src)
    page2 = core.words(src, page=2)
    assert {r[0] for r in page2} == {2}
    assert len(page2) < len(all_rows)


# --------------------------------------------------------------------------- #
# metadata / form fields
# --------------------------------------------------------------------------- #


def test_metadata_title_author() -> None:
    meta = core.pdf_metadata(_bytes(fx.make_meta_pdf()))
    assert meta is not None
    assert meta["Title"] == fx.KNOWN_TITLE
    assert meta["Author"] == fx.KNOWN_AUTHOR


def test_form_fields_known_value() -> None:
    fields = core.form_fields(_bytes(fx.make_form_pdf()))
    assert fields is not None
    assert fields[fx.FORM_FIELD_NAME] == fx.FORM_FIELD_VALUE


def test_form_fields_empty_for_plain_pdf() -> None:
    fields = core.form_fields(_bytes(fx.make_words_pdf()))
    assert fields == {}


# --------------------------------------------------------------------------- #
# render_page — PNG magic bytes + bounds.
# --------------------------------------------------------------------------- #


def test_render_page_png_magic() -> None:
    png = core.render_page(_bytes(fx.make_words_pdf()), 1)
    assert png is not None
    assert png[:8] == PNG_MAGIC
    assert len(png) > 100


def test_render_page_dpi_changes_size() -> None:
    src = _bytes(fx.make_words_pdf())
    small = core.render_page(src, 1, 36)
    big = core.render_page(src, 1, 200)
    assert small is not None and big is not None
    assert len(big) > len(small)


def test_render_page_dpi_clamped() -> None:
    src = _bytes(fx.make_words_pdf())
    # A huge DPI is clamped to MAX_RENDER_DPI, so it equals rendering at the cap.
    huge = core.render_page(src, 1, 100_000)
    capped = core.render_page(src, 1, core.MAX_RENDER_DPI)
    assert huge is not None and capped is not None
    assert huge == capped


def test_render_page_out_of_range() -> None:
    src = _bytes(fx.make_words_pdf())
    assert core.render_page(src, 99) is None
    assert core.render_page(src, 0) is None


# --------------------------------------------------------------------------- #
# encryption — detected, never a hang, graceful elsewhere.
# --------------------------------------------------------------------------- #


def test_encrypted_detected() -> None:
    src = _bytes(fx.make_encrypted_pdf())
    assert core.is_encrypted(src) is True


def test_plain_not_encrypted() -> None:
    assert core.is_encrypted(_bytes(fx.make_words_pdf())) is False


def test_encrypted_other_scalars_graceful() -> None:
    src = _bytes(fx.make_encrypted_pdf())
    # No password -> can't read structure, but NULL not crash.
    assert core.page_count(src) is None
    assert core.pdf_metadata(src) is None
    assert core.render_page(src, 1) is None
    # Table extractors raise a clean PdfError (surfaced as a DuckDB error).
    with pytest.raises(PdfError):
        core.tables(src)


# --------------------------------------------------------------------------- #
# hostile / garbage / empty / NULL-ish input — survive, never crash.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("data", [fx.make_garbage_bytes(), b"", b"%PDF-1.4 broken"])
def test_garbage_scalars_return_none(data: bytes) -> None:
    src = _bytes(data)
    assert core.page_count(src) is None
    assert core.is_encrypted(src) is None
    assert core.pdf_metadata(src) is None
    assert core.form_fields(src) is None
    assert core.render_page(src, 1) is None


@pytest.mark.parametrize("data", [fx.make_garbage_bytes(), b"", b"%PDF-1.4 broken"])
def test_garbage_tables_raise_clean(data: bytes) -> None:
    src = _bytes(data)
    with pytest.raises(PdfError):
        core.tables(src)
    with pytest.raises(PdfError):
        core.words(src)
    with pytest.raises(PdfError):
        core.pages(src)


def test_null_source_is_none() -> None:
    assert PdfSource.from_path(None) is None
    assert PdfSource.from_bytes(None) is None


def test_path_source_missing_file() -> None:
    src = PdfSource.from_path("/no/such/file/here.pdf")
    assert src is not None
    # scalars swallow this to None
    assert core.page_count(src) is None
    # table extractors surface PdfError
    with pytest.raises(PdfError):
        core.pages(src)
