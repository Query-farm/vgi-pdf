"""Pure PDF structure-extraction logic — no Arrow, no VGI, directly unit-testable.

This module is the heart of the worker: it opens a PDF (from a filesystem path
*or* raw bytes) and extracts **structure** — page counts, geometry, word
bounding boxes, tables, form fields, metadata — and renders pages to PNG. It is
deliberately *not* a text extractor (that is ``vgi-tika``); everything here is
about **layout / coordinates / tables / rendering**.

Library choices (all permissive — this matters for a commercial marketplace; see
the README "Why not PyMuPDF" note):

- ``pdfplumber`` (MIT, on ``pdfminer.six`` MIT) — words + boxes, tables, geometry.
- ``pypdfium2`` (Apache-2.0 / BSD-3, Google PDFium) — page rendering to PNG.
- ``pikepdf`` (MPL-2.0) — robust metadata, AcroForm fields, encryption detection.

PDFs are hostile input. **Every** parse/render is wrapped so a malformed,
encrypted, or "bomb" PDF can never crash the worker: callers get ``None`` /
empty results, never an exception that escapes this module (except the few
``open_*`` helpers, which raise :class:`PdfError` for table functions to surface
as a clean error row — they never crash the process either).

Bounds (defence against resource-exhaustion / "bomb" PDFs):

- ``MAX_RENDER_DPI`` caps render resolution so a single page can't blow up RAM.
- ``DEFAULT_DPI`` is the modest default when no DPI is supplied.
- ``MAX_RENDER_PIXELS`` caps the output bitmap area (width*height) per page.

No external resources are ever resolved: we never follow URLs, remote XObjects,
or embedded references — only the bytes we were handed are read.
"""

from __future__ import annotations

import io
import os
from typing import Any

# Heavy libraries are imported once, at module load, and reused for the whole
# process lifetime (worker processes are long-lived). Do NOT import these
# per-row.
import pdfplumber
import pikepdf
import pypdfium2 as pdfium

__all__ = [
    "DEFAULT_DPI",
    "MAX_RENDER_DPI",
    "MAX_RENDER_PIXELS",
    "PdfError",
    "PdfSource",
    "form_fields",
    "is_encrypted",
    "page_count",
    "pages",
    "pdf_metadata",
    "render_page",
    "tables",
    "words",
]

# ---------------------------------------------------------------------------
# Bounds (resource caps for hostile / "bomb" PDFs).
# ---------------------------------------------------------------------------
DEFAULT_DPI = 150
MAX_RENDER_DPI = 300
# ~ A4 at 300 DPI is roughly 2480x3508 ≈ 8.7M px; allow a little headroom.
MAX_RENDER_PIXELS = 25_000_000


class PdfError(Exception):
    """A PDF could not be opened/parsed — surfaced cleanly, never a crash.

    Scalars translate this (and any other failure) to ``NULL``; table functions
    surface a clear error to DuckDB rather than letting the worker die.
    """


# ---------------------------------------------------------------------------
# Polymorphic input: a VARCHAR filesystem path OR a BLOB of PDF bytes.
# ---------------------------------------------------------------------------


class PdfSource:
    """A PDF to read: either a filesystem ``path`` or in-memory ``data`` bytes.

    Exactly one of ``path`` / ``data`` is set. ``read_bytes()`` returns the raw
    PDF bytes regardless of which it is, reading the file lazily. Only the bytes
    we were given (or the local file path we were given) are ever touched — no
    URLs, no remote references.
    """

    __slots__ = ("data", "path")

    def __init__(self, *, path: str | None = None, data: bytes | None = None) -> None:
        self.path = path
        self.data = data

    @classmethod
    def from_path(cls, path: str | None) -> PdfSource | None:
        """Build a source from a VARCHAR path, or ``None`` for a NULL path."""
        if path is None:
            return None
        return cls(path=path)

    @classmethod
    def from_bytes(cls, data: bytes | None) -> PdfSource | None:
        """Build a source from BLOB bytes, or ``None`` for NULL bytes."""
        if data is None:
            return None
        return cls(data=data)

    def read_bytes(self) -> bytes:
        """Return the raw PDF bytes (reading the file path lazily)."""
        if self.data is not None:
            return self.data
        assert self.path is not None
        # Reject absurd paths early; never resolve anything but a local file.
        if not os.path.isfile(self.path):
            raise PdfError(f"not a file: {self.path!r}")
        with open(self.path, "rb") as fh:
            return fh.read()


# ---------------------------------------------------------------------------
# Open helpers — one per backend. Each raises PdfError on failure so callers
# decide whether to map to NULL (scalars) or an error (tables).
# ---------------------------------------------------------------------------


def _open_pikepdf(src: PdfSource) -> pikepdf.Pdf:
    """Open with pikepdf (metadata / forms / encryption). Raises PdfError."""
    try:
        return pikepdf.Pdf.open(io.BytesIO(src.read_bytes()))
    except pikepdf.PasswordError as exc:  # encrypted, no password
        raise PdfError("PDF is encrypted (no password)") from exc
    except PdfError:
        raise
    except Exception as exc:  # malformed / not a PDF / anything
        raise PdfError(f"could not open PDF: {exc}") from exc


def _open_pdfplumber(src: PdfSource) -> pdfplumber.PDF:
    """Open with pdfplumber (words / tables / geometry). Raises PdfError."""
    try:
        return pdfplumber.open(io.BytesIO(src.read_bytes()))
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError(f"could not open PDF: {exc}") from exc


def _open_pdfium(src: PdfSource) -> pdfium.PdfDocument:
    """Open with pypdfium2 (rendering). Raises PdfError."""
    try:
        return pdfium.PdfDocument(src.read_bytes())
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError(f"could not open PDF for rendering: {exc}") from exc


# ---------------------------------------------------------------------------
# Scalars (one value in, one value out). These NEVER raise: any failure on a
# hostile PDF becomes ``None`` so the per-row scalar yields SQL NULL.
# ---------------------------------------------------------------------------


def page_count(src: PdfSource) -> int | None:
    """Number of pages, or ``None`` if the PDF can't be read."""
    try:
        pdf = _open_pikepdf(src)
        try:
            return len(pdf.pages)
        finally:
            pdf.close()
    except Exception:
        return None


def is_encrypted(src: PdfSource) -> bool | None:
    """``True`` if the PDF is encrypted, ``False`` if not, ``None`` if unreadable.

    An encrypted PDF is *not* an error here — detecting encryption is the whole
    point. ``pikepdf.Pdf.open`` raises ``PasswordError`` for an encrypted file
    opened without a password, which we treat as "encrypted == True" rather than
    a failure (and never as a hang).
    """
    try:
        data = src.read_bytes()
    except Exception:
        return None
    try:
        pdf = pikepdf.Pdf.open(io.BytesIO(data))
        try:
            return bool(pdf.is_encrypted)
        finally:
            pdf.close()
    except pikepdf.PasswordError:
        # Encrypted and we have no password — definitively encrypted.
        return True
    except Exception:
        # Not a readable PDF at all.
        return None


def _stringify(value: Any) -> str:
    """Best-effort string form of a pikepdf/metadata value."""
    try:
        return str(value)
    except Exception:
        return ""


def pdf_metadata(src: PdfSource) -> dict[str, str] | None:
    """Document metadata as a ``{key: value}`` dict, or ``None`` if unreadable.

    Prefers the XMP/docinfo via pikepdf. Keys are the human-friendly docinfo
    names with the leading ``/`` stripped (e.g. ``Title``, ``Author``,
    ``Producer``, ``CreationDate``). Empty/missing → key omitted.
    """
    try:
        pdf = _open_pikepdf(src)
    except Exception:
        return None
    try:
        out: dict[str, str] = {}
        try:
            docinfo = pdf.docinfo
        except Exception:
            docinfo = None
        if docinfo is not None:
            for key, value in docinfo.items():
                name = str(key)
                if name.startswith("/"):
                    name = name[1:]
                text = _stringify(value)
                if text:
                    out[name] = text
        return out
    except Exception:
        return None
    finally:
        pdf.close()


def _field_value(obj: Any) -> str | None:
    """Extract an AcroForm field's value as a string, or ``None``."""
    try:
        if "/V" not in obj:
            return None
        return _stringify(obj["/V"])
    except Exception:
        return None


def form_fields(src: PdfSource) -> dict[str, str] | None:
    """AcroForm field ``{name: value}`` map, or ``None`` if unreadable.

    Returns an empty dict for a valid PDF that simply has no form fields.
    """
    try:
        pdf = _open_pikepdf(src)
    except Exception:
        return None
    try:
        out: dict[str, str] = {}
        root = pdf.Root
        if "/AcroForm" not in root:
            return out
        acro = root["/AcroForm"]
        if "/Fields" not in acro:
            return out
        for fld in acro["/Fields"]:
            try:
                name_obj = fld.get("/T")
                if name_obj is None:
                    continue
                name = _stringify(name_obj)
                value = _field_value(fld)
                if name:
                    out[name] = value if value is not None else ""
            except Exception:
                continue
        return out
    except Exception:
        return None
    finally:
        pdf.close()


def _clamp_dpi(dpi: int | None) -> int:
    """Clamp a requested DPI into ``[1, MAX_RENDER_DPI]`` (default if None)."""
    if dpi is None:
        return DEFAULT_DPI
    if dpi < 1:
        return 1
    if dpi > MAX_RENDER_DPI:
        return MAX_RENDER_DPI
    return dpi


def render_page(src: PdfSource, page: int | None, dpi: int | None = None) -> bytes | None:
    """Render a single (1-based) page to a PNG ``bytes`` blob, or ``None``.

    ``dpi`` defaults to :data:`DEFAULT_DPI` and is clamped to
    :data:`MAX_RENDER_DPI`. The rasterised bitmap area is bounded by
    :data:`MAX_RENDER_PIXELS`; an over-large page is rendered at a reduced scale
    instead of being allowed to exhaust memory. Returns ``None`` on any failure
    (unreadable PDF, encrypted-no-password, out-of-range page).
    """
    if page is None:
        return None
    eff_dpi = _clamp_dpi(dpi)
    try:
        doc = _open_pdfium(src)
    except Exception:
        return None
    try:
        n = len(doc)
        if page < 1 or page > n:
            return None
        scale = eff_dpi / 72.0
        pdf_page = doc[page - 1]
        try:
            width_pt = pdf_page.get_width()
            height_pt = pdf_page.get_height()
            # Bound the output bitmap area; shrink scale if a page is enormous.
            px_area = (width_pt * scale) * (height_pt * scale)
            if px_area > MAX_RENDER_PIXELS and px_area > 0:
                scale *= (MAX_RENDER_PIXELS / px_area) ** 0.5
            bitmap = pdf_page.render(scale=scale)
            try:
                pil_image = bitmap.to_pil()
                buf = io.BytesIO()
                pil_image.save(buf, format="PNG")
                return buf.getvalue()
            finally:
                bitmap.close()
        finally:
            pdf_page.close()
    except Exception:
        return None
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Table-shaped extractors. These return lists of plain tuples. They raise
# PdfError on an unopenable PDF (so the table function surfaces a clean error);
# a NULL input is handled by the caller (no rows).
# ---------------------------------------------------------------------------


def pages(src: PdfSource) -> list[tuple[int, float, float, int]]:
    """Per-page geometry as ``(page, width, height, rotation)`` rows (1-based)."""
    pdf = _open_pdfplumber(src)
    try:
        rows: list[tuple[int, float, float, int]] = []
        for idx, pg in enumerate(pdf.pages, start=1):
            try:
                width = float(pg.width)
                height = float(pg.height)
                rotation = int(pg.rotation or 0)
            except Exception:
                continue
            rows.append((idx, width, height, rotation))
        return rows
    finally:
        pdf.close()


def words(src: PdfSource, page: int | None = None) -> list[tuple[int, str, float, float, float, float]]:
    """Per-word boxes ``(page, text, x0, top, x1, bottom)``.

    If ``page`` is given (1-based), only that page's words are returned; an
    out-of-range page yields no rows. A page whose word extraction fails is
    skipped rather than aborting the whole call.
    """
    pdf = _open_pdfplumber(src)
    try:
        rows: list[tuple[int, str, float, float, float, float]] = []
        for idx, pg in enumerate(pdf.pages, start=1):
            if page is not None and idx != page:
                continue
            try:
                for w in pg.extract_words():
                    rows.append(
                        (
                            idx,
                            str(w.get("text", "")),
                            float(w["x0"]),
                            float(w["top"]),
                            float(w["x1"]),
                            float(w["bottom"]),
                        )
                    )
            except Exception:
                continue
        return rows
    finally:
        pdf.close()


def tables(src: PdfSource, page: int | None = None) -> list[tuple[int, int, int, int, str | None]]:
    """Long-format table cells ``(page, table_index, row_index, col, value)``.

    Every PDF table is decomposed into one row per cell so the result is a tidy
    relational shape. ``table_index`` is 0-based within a page; ``row_index`` /
    ``col`` are 0-based within a table. A missing cell value is ``None``. If
    ``page`` is given (1-based), only that page is scanned.
    """
    pdf = _open_pdfplumber(src)
    try:
        rows: list[tuple[int, int, int, int, str | None]] = []
        for idx, pg in enumerate(pdf.pages, start=1):
            if page is not None and idx != page:
                continue
            try:
                extracted = pg.extract_tables()
            except Exception:
                continue
            for t_index, table in enumerate(extracted):
                for r_index, table_row in enumerate(table):
                    for c_index, cell in enumerate(table_row):
                        value = None if cell is None else str(cell)
                        rows.append((idx, t_index, r_index, c_index, value))
        return rows
    finally:
        pdf.close()
