"""Per-row scalar PDF functions.

Every function here is a true DuckDB **scalar** -- one PDF (per row) in, one
value out -- so it can be used inline in any projection:

    SELECT pdf.page_count(path)        FROM documents;
    SELECT pdf.is_encrypted(blob)      FROM uploads;
    SELECT pdf.pdf_metadata(path)['Title'] FROM documents;
    SELECT pdf.render_page(path, 1)    FROM documents;
    SELECT pdf.render_page(path, 1, 72) FROM documents;

Polymorphic input + argument syntax
------------------------------------
VGI / DuckDB *scalar* functions take **positional** arguments and resolve
overloads by the *types* and *arity* of those arguments (the ``name := value``
named-argument syntax is a property of table functions, not scalars). Each
function therefore accepts the ``pdf`` argument as **either**:

- a ``VARCHAR`` filesystem path the worker opens, or
- a ``BLOB`` of the raw PDF bytes (travelling over Arrow as binary).

These are two distinct DuckDB signatures, so each is its own ``ScalarFunction``
subclass sharing the ``Meta.name`` -- the same overload idiom the sibling
``vgi-conform`` worker uses for optional arguments. ``render_page`` additionally
has an optional ``dpi`` (default 150), so it comes in four overloads:
``(path, page)`` / ``(path, page, dpi)`` / ``(blob, page)`` / ``(blob, page, dpi)``.

NULL / hostile semantics: a NULL ``pdf`` input yields NULL output; a malformed,
encrypted, or otherwise unreadable PDF also yields NULL -- never a crash and
never a hang. (Encrypted detection is the one case where "encrypted" is a
*successful* answer of ``true``, not a failure.)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

import pyarrow as pa
from vgi.arguments import ConstParam, Param, Returns
from vgi.metadata import FunctionExample
from vgi.scalar_function import ScalarFunction

from . import core
from .core import PdfSource

# ---------------------------------------------------------------------------
# Mapping helpers: apply a pure ``PdfSource -> X`` function across an input
# array of paths (strings) or bytes (binary), passing NULL straight through.
# ---------------------------------------------------------------------------

_MAP_TYPE = pa.map_(pa.string(), pa.string())


def _sources_from_paths(arr: pa.StringArray) -> list[PdfSource | None]:
    return [PdfSource.from_path(x) for x in arr.to_pylist()]


def _sources_from_bytes(arr: pa.BinaryArray) -> list[PdfSource | None]:
    return [PdfSource.from_bytes(x) for x in arr.to_pylist()]


def _map(srcs: list[PdfSource | None], fn: Callable[[PdfSource], Any], arrow_type: pa.DataType) -> pa.Array:
    out = [None if s is None else fn(s) for s in srcs]
    return pa.array(out, type=arrow_type)


def _map_map(srcs: list[PdfSource | None], fn: Callable[[PdfSource], dict[str, str] | None]) -> pa.Array:
    out: list[list[tuple[str, str]] | None] = []
    for s in srcs:
        if s is None:
            out.append(None)
            continue
        result = fn(s)
        out.append(None if result is None else list(result.items()))
    return pa.array(out, type=_MAP_TYPE)


# ===========================================================================
# page_count(pdf) -> INT
# ===========================================================================


class PageCountPathFunction(ScalarFunction):
    """``page_count(path)`` -- pages in the PDF at a filesystem path."""

    class Meta:
        name = "page_count"
        description = "Number of pages in a PDF (VARCHAR path), or NULL if unreadable"
        categories = ["pdf", "structure"]
        examples = [
            FunctionExample(
                sql="SELECT pdf.page_count('doc.pdf')",
                description="Page count of a PDF file",
            ),
        ]

    @classmethod
    def compute(
        cls, pdf: Annotated[pa.StringArray, Param(doc="Filesystem path to a PDF.")]
    ) -> Annotated[pa.Int32Array, Returns(arrow_type=pa.int32())]:
        return _map(_sources_from_paths(pdf), core.page_count, pa.int32())


class PageCountBytesFunction(ScalarFunction):
    """``page_count(blob)`` -- pages in a PDF passed as raw bytes."""

    class Meta:
        name = "page_count"
        description = "Number of pages in a PDF (BLOB bytes), or NULL if unreadable"
        categories = ["pdf", "structure"]
        examples = [
            FunctionExample(
                sql="SELECT pdf.page_count(blob) FROM uploads",
                description="Page count of a PDF held as bytes",
            ),
        ]

    @classmethod
    def compute(
        cls, pdf: Annotated[pa.BinaryArray, Param(doc="Raw PDF bytes.", arrow_type=pa.binary())]
    ) -> Annotated[pa.Int32Array, Returns(arrow_type=pa.int32())]:
        return _map(_sources_from_bytes(pdf), core.page_count, pa.int32())


# ===========================================================================
# is_encrypted(pdf) -> BOOLEAN
# ===========================================================================


class IsEncryptedPathFunction(ScalarFunction):
    """``is_encrypted(path)`` -- True if the PDF at a path is encrypted."""

    class Meta:
        name = "is_encrypted"
        description = "True if the PDF (VARCHAR path) is encrypted, NULL if unreadable"
        categories = ["pdf", "security"]
        examples = [
            FunctionExample(
                sql="SELECT pdf.is_encrypted('secret.pdf')",
                description="Whether a PDF file is encrypted",
            ),
        ]

    @classmethod
    def compute(
        cls, pdf: Annotated[pa.StringArray, Param(doc="Filesystem path to a PDF.")]
    ) -> Annotated[pa.BooleanArray, Returns(arrow_type=pa.bool_())]:
        return _map(_sources_from_paths(pdf), core.is_encrypted, pa.bool_())


class IsEncryptedBytesFunction(ScalarFunction):
    """``is_encrypted(blob)`` -- True if a PDF passed as bytes is encrypted."""

    class Meta:
        name = "is_encrypted"
        description = "True if the PDF (BLOB bytes) is encrypted, NULL if unreadable"
        categories = ["pdf", "security"]
        examples = [
            FunctionExample(
                sql="SELECT pdf.is_encrypted(blob) FROM uploads",
                description="Whether a PDF held as bytes is encrypted",
            ),
        ]

    @classmethod
    def compute(
        cls, pdf: Annotated[pa.BinaryArray, Param(doc="Raw PDF bytes.", arrow_type=pa.binary())]
    ) -> Annotated[pa.BooleanArray, Returns(arrow_type=pa.bool_())]:
        return _map(_sources_from_bytes(pdf), core.is_encrypted, pa.bool_())


# ===========================================================================
# pdf_metadata(pdf) -> MAP(VARCHAR, VARCHAR)
# ===========================================================================


class PdfMetadataPathFunction(ScalarFunction):
    """``pdf_metadata(path)`` -- document metadata map for a PDF at a path."""

    class Meta:
        name = "pdf_metadata"
        description = "Document metadata (Title/Author/Producer/...) of a PDF (VARCHAR path)"
        categories = ["pdf", "metadata"]
        examples = [
            FunctionExample(
                sql="SELECT pdf.pdf_metadata('doc.pdf')['Title']",
                description="Title from a PDF's metadata",
            ),
        ]

    @classmethod
    def compute(
        cls, pdf: Annotated[pa.StringArray, Param(doc="Filesystem path to a PDF.")]
    ) -> Annotated[pa.Array, Returns(arrow_type=_MAP_TYPE)]:
        return _map_map(_sources_from_paths(pdf), core.pdf_metadata)


class PdfMetadataBytesFunction(ScalarFunction):
    """``pdf_metadata(blob)`` -- document metadata map for PDF bytes."""

    class Meta:
        name = "pdf_metadata"
        description = "Document metadata (Title/Author/Producer/...) of a PDF (BLOB bytes)"
        categories = ["pdf", "metadata"]
        examples = [
            FunctionExample(
                sql="SELECT pdf.pdf_metadata(blob)['Author'] FROM uploads",
                description="Author from PDF bytes' metadata",
            ),
        ]

    @classmethod
    def compute(
        cls, pdf: Annotated[pa.BinaryArray, Param(doc="Raw PDF bytes.", arrow_type=pa.binary())]
    ) -> Annotated[pa.Array, Returns(arrow_type=_MAP_TYPE)]:
        return _map_map(_sources_from_bytes(pdf), core.pdf_metadata)


# ===========================================================================
# form_fields(pdf) -> MAP(VARCHAR, VARCHAR)
# ===========================================================================


class FormFieldsPathFunction(ScalarFunction):
    """``form_fields(path)`` -- AcroForm field name->value map (PDF at a path)."""

    class Meta:
        name = "form_fields"
        description = "AcroForm field name->value map of a PDF (VARCHAR path)"
        categories = ["pdf", "forms"]
        examples = [
            FunctionExample(
                sql="SELECT pdf.form_fields('form.pdf')",
                description="Form fields of a fillable PDF",
            ),
        ]

    @classmethod
    def compute(
        cls, pdf: Annotated[pa.StringArray, Param(doc="Filesystem path to a PDF.")]
    ) -> Annotated[pa.Array, Returns(arrow_type=_MAP_TYPE)]:
        return _map_map(_sources_from_paths(pdf), core.form_fields)


class FormFieldsBytesFunction(ScalarFunction):
    """``form_fields(blob)`` -- AcroForm field name->value map (PDF bytes)."""

    class Meta:
        name = "form_fields"
        description = "AcroForm field name->value map of a PDF (BLOB bytes)"
        categories = ["pdf", "forms"]
        examples = [
            FunctionExample(
                sql="SELECT pdf.form_fields(blob) FROM uploads",
                description="Form fields of a fillable PDF held as bytes",
            ),
        ]

    @classmethod
    def compute(
        cls, pdf: Annotated[pa.BinaryArray, Param(doc="Raw PDF bytes.", arrow_type=pa.binary())]
    ) -> Annotated[pa.Array, Returns(arrow_type=_MAP_TYPE)]:
        return _map_map(_sources_from_bytes(pdf), core.form_fields)


# ===========================================================================
# render_page(pdf, page[, dpi]) -> BLOB (PNG)
#
# Four overloads: path/bytes x default-dpi/explicit-dpi. ``page`` is a per-row
# Int32 column; ``dpi`` is a constant argument when supplied.
# ===========================================================================


def _render(srcs: list[PdfSource | None], pages: pa.Int32Array, dpi: int | None) -> pa.Array:
    page_list = pages.to_pylist()
    out: list[bytes | None] = []
    for s, pg in zip(srcs, page_list, strict=True):
        if s is None or pg is None:
            out.append(None)
        else:
            out.append(core.render_page(s, pg, dpi))
    return pa.array(out, type=pa.binary())


class RenderPagePathFunction(ScalarFunction):
    """``render_page(path, page)`` -- PNG of one page at the default DPI."""

    class Meta:
        name = "render_page"
        description = "Render one (1-based) page of a PDF (VARCHAR path) to a PNG BLOB (default 150 DPI)"
        categories = ["pdf", "render"]
        examples = [
            FunctionExample(
                sql="SELECT pdf.render_page('doc.pdf', 1)",
                description="Render the first page to a PNG",
            ),
        ]

    @classmethod
    def compute(
        cls,
        pdf: Annotated[pa.StringArray, Param(doc="Filesystem path to a PDF.")],
        page: Annotated[pa.Int32Array, Param(doc="1-based page number.", arrow_type=pa.int32())],
    ) -> Annotated[pa.BinaryArray, Returns(arrow_type=pa.binary())]:
        return _render(_sources_from_paths(pdf), page, None)


class RenderPagePathDpiFunction(ScalarFunction):
    """``render_page(path, page, dpi)`` -- PNG of one page at a given DPI."""

    class Meta:
        name = "render_page"
        description = "Render one page of a PDF (VARCHAR path) to a PNG BLOB at a given DPI (capped at 300)"
        categories = ["pdf", "render"]
        examples = [
            FunctionExample(
                sql="SELECT pdf.render_page('doc.pdf', 1, 72)",
                description="Render the first page at 72 DPI",
            ),
        ]

    @classmethod
    def compute(
        cls,
        pdf: Annotated[pa.StringArray, Param(doc="Filesystem path to a PDF.")],
        page: Annotated[pa.Int32Array, Param(doc="1-based page number.", arrow_type=pa.int32())],
        dpi: Annotated[int, ConstParam("Render resolution in DPI (capped at 300).", arrow_type=pa.int32())],
    ) -> Annotated[pa.BinaryArray, Returns(arrow_type=pa.binary())]:
        return _render(_sources_from_paths(pdf), page, dpi)


class RenderPageBytesFunction(ScalarFunction):
    """``render_page(blob, page)`` -- PNG of one page (PDF bytes), default DPI."""

    class Meta:
        name = "render_page"
        description = "Render one (1-based) page of a PDF (BLOB bytes) to a PNG BLOB (default 150 DPI)"
        categories = ["pdf", "render"]
        examples = [
            FunctionExample(
                sql="SELECT pdf.render_page(blob, 1) FROM uploads",
                description="Render the first page of PDF bytes to a PNG",
            ),
        ]

    @classmethod
    def compute(
        cls,
        pdf: Annotated[pa.BinaryArray, Param(doc="Raw PDF bytes.", arrow_type=pa.binary())],
        page: Annotated[pa.Int32Array, Param(doc="1-based page number.", arrow_type=pa.int32())],
    ) -> Annotated[pa.BinaryArray, Returns(arrow_type=pa.binary())]:
        return _render(_sources_from_bytes(pdf), page, None)


class RenderPageBytesDpiFunction(ScalarFunction):
    """``render_page(blob, page, dpi)`` -- PNG of one page (bytes) at a DPI."""

    class Meta:
        name = "render_page"
        description = "Render one page of a PDF (BLOB bytes) to a PNG BLOB at a given DPI (capped at 300)"
        categories = ["pdf", "render"]
        examples = [
            FunctionExample(
                sql="SELECT pdf.render_page(blob, 1, 72) FROM uploads",
                description="Render the first page of PDF bytes at 72 DPI",
            ),
        ]

    @classmethod
    def compute(
        cls,
        pdf: Annotated[pa.BinaryArray, Param(doc="Raw PDF bytes.", arrow_type=pa.binary())],
        page: Annotated[pa.Int32Array, Param(doc="1-based page number.", arrow_type=pa.int32())],
        dpi: Annotated[int, ConstParam("Render resolution in DPI (capped at 300).", arrow_type=pa.int32())],
    ) -> Annotated[pa.BinaryArray, Returns(arrow_type=pa.binary())]:
        return _render(_sources_from_bytes(pdf), page, dpi)


SCALAR_FUNCTIONS: list[type] = [
    PageCountPathFunction,
    PageCountBytesFunction,
    IsEncryptedPathFunction,
    IsEncryptedBytesFunction,
    PdfMetadataPathFunction,
    PdfMetadataBytesFunction,
    FormFieldsPathFunction,
    FormFieldsBytesFunction,
    RenderPagePathFunction,
    RenderPagePathDpiFunction,
    RenderPageBytesFunction,
    RenderPageBytesDpiFunction,
]
