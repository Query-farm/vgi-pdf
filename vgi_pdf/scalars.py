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
from .meta import object_tags

# ---------------------------------------------------------------------------
# Per-object discovery/description tags (VGI112/113/124/126/128).
#
# A path overload and a bytes overload of the same function share one
# ``Meta.name`` and therefore collapse to a single catalog object, so both
# overloads carry the SAME tag dict (the linter sees one ``pdf.main.<name>``).
# Each ``vgi.doc_llm`` / ``vgi.doc_md`` pair is DISTINCT narrative content.
# ---------------------------------------------------------------------------

_SRC = "vgi_pdf/scalars.py"

_PAGE_COUNT_TAGS = object_tags(
    title="Count PDF Pages",
    doc_llm=(
        "# page_count\n\n"
        "Return the **number of pages** in a PDF as an `INTEGER`. The `pdf` argument is either a "
        "`VARCHAR` filesystem path the worker opens or a `BLOB` of the raw PDF bytes.\n\n"
        "Use it to size a document before paging through `pages`, `words`, or `tables`, to validate "
        "an upload, or to drive a `generate_series(1, page_count(...))` loop over `render_page`.\n\n"
        "**Edge cases:** a NULL input returns NULL; a malformed, encrypted-beyond-reading, or "
        "otherwise unreadable PDF also returns NULL rather than raising -- this scalar never throws "
        "and never hangs."
    ),
    doc_md=(
        "# Count PDF Pages\n\n"
        "Counts the pages in a PDF document.\n\n"
        "## Usage\n\n"
        "```sql\n"
        "SELECT pdf.page_count('report.pdf');        -- 12\n"
        "SELECT pdf.page_count(blob) FROM uploads;   -- per-row page counts\n"
        "```\n\n"
        "Accepts a `VARCHAR` path or a `BLOB` of bytes and returns an `INTEGER`.\n\n"
        "## Notes\n\n"
        "Returns `NULL` for a NULL input or a PDF that cannot be read. Useful for sizing a document "
        "ahead of the `pages`, `words`, and `tables` table functions."
    ),
    keywords="page count, number of pages, pages, count, length, size, npages, pdf",
    relative_path=_SRC,
)

_IS_ENCRYPTED_TAGS = object_tags(
    title="Detect PDF Encryption",
    doc_llm=(
        "# is_encrypted\n\n"
        "Return `TRUE` if the PDF is **encrypted** (password/permissions protected), `FALSE` if it is "
        "plain, or `NULL` if the bytes are not a readable PDF at all. The `pdf` argument is a `VARCHAR` "
        "path or a `BLOB` of bytes.\n\n"
        "Use it to triage a batch of documents before processing -- encrypted files cannot be parsed "
        "for words/tables/metadata without a password.\n\n"
        "**Key behavior:** an encrypted-with-no-password file is reported as `TRUE` (encryption is a "
        "*successful* answer, not an error); the function never attempts to brute-force a password and "
        "never hangs. NULL input yields NULL."
    ),
    doc_md=(
        "# Detect PDF Encryption\n\n"
        "Reports whether a PDF is encrypted.\n\n"
        "## Usage\n\n"
        "```sql\n"
        "SELECT pdf.is_encrypted('secret.pdf');   -- true\n"
        "SELECT pdf.is_encrypted(blob) FROM uploads WHERE pdf.is_encrypted(blob);\n"
        "```\n\n"
        "Returns a `BOOLEAN`; accepts a `VARCHAR` path or a `BLOB`.\n\n"
        "## Notes\n\n"
        "`TRUE` means the document is password/permissions protected. `NULL` means the input was not a "
        "readable PDF. Detection is purely structural -- no password guessing is performed."
    ),
    keywords="encrypted, encryption, password, protected, security, locked, acroform, permissions",
    relative_path=_SRC,
)

_PDF_METADATA_TAGS = object_tags(
    title="Read PDF Document Metadata",
    doc_llm=(
        "# pdf_metadata\n\n"
        "Return the PDF's **document information dictionary** as a `MAP(VARCHAR, VARCHAR)` -- keys such "
        "as `Title`, `Author`, `Subject`, `Creator`, `Producer`, `CreationDate`, and `ModDate`. The "
        "`pdf` argument is a `VARCHAR` path or a `BLOB`.\n\n"
        "Index a single field with `pdf_metadata(...)['Title']`, or expand the whole map with "
        "`UNNEST(map_entries(...))`. Use it to catalog documents, sort by author, or extract titles.\n\n"
        "**Edge cases:** keys present depend on the producer (any subset may be missing); a NULL input "
        "or an unreadable PDF returns NULL. Never raises."
    ),
    doc_md=(
        "# Read PDF Document Metadata\n\n"
        "Extracts the document information dictionary (Title, Author, Producer, dates, ...).\n\n"
        "## Usage\n\n"
        "```sql\n"
        "SELECT pdf.pdf_metadata('report.pdf')['Title'];\n"
        "SELECT key, value FROM (SELECT UNNEST(map_entries(pdf.pdf_metadata('report.pdf'))));\n"
        "```\n\n"
        "Returns a `MAP(VARCHAR, VARCHAR)`; accepts a `VARCHAR` path or a `BLOB`.\n\n"
        "## Notes\n\n"
        "Available keys vary by the tool that produced the PDF. Returns `NULL` for a NULL input or an "
        "unreadable document."
    ),
    keywords="metadata, document info, title, author, producer, creator, subject, creation date, properties",
    relative_path=_SRC,
)

_FORM_FIELDS_TAGS = object_tags(
    title="Extract PDF Form Fields",
    doc_llm=(
        "# form_fields\n\n"
        "Return the values of an **AcroForm** (fillable PDF form) as a `MAP(VARCHAR, VARCHAR)` mapping "
        "each field's fully-qualified name to its current value. The `pdf` argument is a `VARCHAR` path "
        "or a `BLOB`.\n\n"
        "Use it to read submitted form data -- applications, invoices, government forms -- straight "
        "into SQL, then pivot fields into columns with `['field_name']` lookups.\n\n"
        "**Edge cases:** a document with no AcroForm yields an empty map; checkbox/radio values are the "
        "export value strings; a NULL input or unreadable PDF returns NULL. Never raises."
    ),
    doc_md=(
        "# Extract PDF Form Fields\n\n"
        "Reads filled-in AcroForm field values from a fillable PDF.\n\n"
        "## Usage\n\n"
        "```sql\n"
        "SELECT pdf.form_fields('application.pdf');\n"
        "SELECT pdf.form_fields('application.pdf')['applicant_name'];\n"
        "```\n\n"
        "Returns a `MAP(VARCHAR, VARCHAR)` of field name to value; accepts a `VARCHAR` path or a "
        "`BLOB`.\n\n"
        "## Notes\n\n"
        "A PDF without a form returns an empty map. Returns `NULL` for a NULL input or an unreadable "
        "document."
    ),
    keywords="form, form fields, acroform, fillable, fields, inputs, checkbox, submission, values",
    relative_path=_SRC,
)

_RENDER_PAGE_TAGS = object_tags(
    title="Render PDF Page to Image",
    doc_llm=(
        "# render_page\n\n"
        "Rasterize **one (1-based) page** of a PDF to a **PNG image** returned as a `BLOB`. Signature "
        "`render_page(pdf, page[, dpi])`: `pdf` is a `VARCHAR` path or a `BLOB`, `page` is the 1-based "
        "page number, and the optional `dpi` sets the resolution (default 150, capped at 300).\n\n"
        "Use it to thumbnail PDFs, preview a page, or feed a page image to a vision model.\n\n"
        "**Bounds & edge cases:** the output bitmap area is capped (an enormous page renders at reduced "
        "scale rather than exhausting memory); a NULL input, NULL page, out-of-range page, or "
        "unreadable PDF returns NULL. Never raises and never OOMs."
    ),
    doc_md=(
        "# Render PDF Page to Image\n\n"
        "Renders a single PDF page to a PNG image.\n\n"
        "## Usage\n\n"
        "```sql\n"
        "SELECT pdf.render_page('doc.pdf', 1);        -- first page at 150 DPI\n"
        "SELECT pdf.render_page('doc.pdf', 1, 72);    -- first page at 72 DPI\n"
        "```\n\n"
        "Returns a PNG `BLOB`. `page` is 1-based; `dpi` defaults to 150 and is capped at 300. Accepts a "
        "`VARCHAR` path or a `BLOB`.\n\n"
        "## Notes\n\n"
        "The rendered bitmap area is capped to avoid runaway memory use. Returns `NULL` for a NULL "
        "input, an out-of-range page, or an unreadable document."
    ),
    keywords="render, render page, png, image, rasterize, thumbnail, preview, screenshot, dpi, bitmap",
    relative_path=_SRC,
)


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
        """Function metadata (name, description, examples)."""

        name = "page_count"
        description = "Number of pages in a PDF (VARCHAR path), or NULL if unreadable"
        categories = ["pdf", "structure"]
        tags = _PAGE_COUNT_TAGS
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
        """Compute the output column for a batch of input rows."""
        return _map(_sources_from_paths(pdf), core.page_count, pa.int32())


class PageCountBytesFunction(ScalarFunction):
    """``page_count(blob)`` -- pages in a PDF passed as raw bytes."""

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "page_count"
        description = "Number of pages in a PDF (BLOB bytes), or NULL if unreadable"
        categories = ["pdf", "structure"]
        tags = _PAGE_COUNT_TAGS
        examples = [
            FunctionExample(
                sql="SELECT pdf.page_count('not-a-pdf'::BLOB)",
                description="Page count of a PDF held as bytes (NULL for non-PDF bytes)",
            ),
        ]

    @classmethod
    def compute(
        cls, pdf: Annotated[pa.BinaryArray, Param(doc="Raw PDF bytes.", arrow_type=pa.binary())]
    ) -> Annotated[pa.Int32Array, Returns(arrow_type=pa.int32())]:
        """Compute the output column for a batch of input rows."""
        return _map(_sources_from_bytes(pdf), core.page_count, pa.int32())


# ===========================================================================
# is_encrypted(pdf) -> BOOLEAN
# ===========================================================================


class IsEncryptedPathFunction(ScalarFunction):
    """``is_encrypted(path)`` -- True if the PDF at a path is encrypted."""

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "is_encrypted"
        description = "True if the PDF (VARCHAR path) is encrypted, NULL if unreadable"
        categories = ["pdf", "security"]
        tags = _IS_ENCRYPTED_TAGS
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
        """Compute the output column for a batch of input rows."""
        return _map(_sources_from_paths(pdf), core.is_encrypted, pa.bool_())


class IsEncryptedBytesFunction(ScalarFunction):
    """``is_encrypted(blob)`` -- True if a PDF passed as bytes is encrypted."""

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "is_encrypted"
        description = "True if the PDF (BLOB bytes) is encrypted, NULL if unreadable"
        categories = ["pdf", "security"]
        tags = _IS_ENCRYPTED_TAGS
        examples = [
            FunctionExample(
                sql="SELECT pdf.is_encrypted('not-a-pdf'::BLOB)",
                description="Whether a PDF held as bytes is encrypted (NULL for non-PDF bytes)",
            ),
        ]

    @classmethod
    def compute(
        cls, pdf: Annotated[pa.BinaryArray, Param(doc="Raw PDF bytes.", arrow_type=pa.binary())]
    ) -> Annotated[pa.BooleanArray, Returns(arrow_type=pa.bool_())]:
        """Compute the output column for a batch of input rows."""
        return _map(_sources_from_bytes(pdf), core.is_encrypted, pa.bool_())


# ===========================================================================
# pdf_metadata(pdf) -> MAP(VARCHAR, VARCHAR)
# ===========================================================================


class PdfMetadataPathFunction(ScalarFunction):
    """``pdf_metadata(path)`` -- document metadata map for a PDF at a path."""

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "pdf_metadata"
        description = "Document metadata (Title/Author/Producer/...) of a PDF (VARCHAR path)"
        categories = ["pdf", "metadata"]
        tags = _PDF_METADATA_TAGS
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
        """Compute the output column for a batch of input rows."""
        return _map_map(_sources_from_paths(pdf), core.pdf_metadata)


class PdfMetadataBytesFunction(ScalarFunction):
    """``pdf_metadata(blob)`` -- document metadata map for PDF bytes."""

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "pdf_metadata"
        description = "Document metadata (Title/Author/Producer/...) of a PDF (BLOB bytes)"
        categories = ["pdf", "metadata"]
        tags = _PDF_METADATA_TAGS
        examples = [
            FunctionExample(
                sql="SELECT pdf.pdf_metadata('not-a-pdf'::BLOB)['Author']",
                description="Author from PDF bytes' metadata (NULL for non-PDF bytes)",
            ),
        ]

    @classmethod
    def compute(
        cls, pdf: Annotated[pa.BinaryArray, Param(doc="Raw PDF bytes.", arrow_type=pa.binary())]
    ) -> Annotated[pa.Array, Returns(arrow_type=_MAP_TYPE)]:
        """Compute the output column for a batch of input rows."""
        return _map_map(_sources_from_bytes(pdf), core.pdf_metadata)


# ===========================================================================
# form_fields(pdf) -> MAP(VARCHAR, VARCHAR)
# ===========================================================================


class FormFieldsPathFunction(ScalarFunction):
    """``form_fields(path)`` -- AcroForm field name->value map (PDF at a path)."""

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "form_fields"
        description = "AcroForm field name->value map of a PDF (VARCHAR path)"
        categories = ["pdf", "forms"]
        tags = _FORM_FIELDS_TAGS
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
        """Compute the output column for a batch of input rows."""
        return _map_map(_sources_from_paths(pdf), core.form_fields)


class FormFieldsBytesFunction(ScalarFunction):
    """``form_fields(blob)`` -- AcroForm field name->value map (PDF bytes)."""

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "form_fields"
        description = "AcroForm field name->value map of a PDF (BLOB bytes)"
        categories = ["pdf", "forms"]
        tags = _FORM_FIELDS_TAGS
        examples = [
            FunctionExample(
                sql="SELECT pdf.form_fields('not-a-pdf'::BLOB)",
                description="Form fields of a fillable PDF held as bytes (NULL for non-PDF bytes)",
            ),
        ]

    @classmethod
    def compute(
        cls, pdf: Annotated[pa.BinaryArray, Param(doc="Raw PDF bytes.", arrow_type=pa.binary())]
    ) -> Annotated[pa.Array, Returns(arrow_type=_MAP_TYPE)]:
        """Compute the output column for a batch of input rows."""
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
        """Function metadata (name, description, examples)."""

        name = "render_page"
        description = "Render one (1-based) page of a PDF (VARCHAR path) to a PNG BLOB (default 150 DPI)"
        categories = ["pdf", "render"]
        tags = _RENDER_PAGE_TAGS
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
        """Compute the output column for a batch of input rows."""
        return _render(_sources_from_paths(pdf), page, None)


class RenderPagePathDpiFunction(ScalarFunction):
    """``render_page(path, page, dpi)`` -- PNG of one page at a given DPI."""

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "render_page"
        description = "Render one page of a PDF (VARCHAR path) to a PNG BLOB at a given DPI (capped at 300)"
        categories = ["pdf", "render"]
        tags = _RENDER_PAGE_TAGS
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
        """Compute the output column for a batch of input rows."""
        return _render(_sources_from_paths(pdf), page, dpi)


class RenderPageBytesFunction(ScalarFunction):
    """``render_page(blob, page)`` -- PNG of one page (PDF bytes), default DPI."""

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "render_page"
        description = "Render one (1-based) page of a PDF (BLOB bytes) to a PNG BLOB (default 150 DPI)"
        categories = ["pdf", "render"]
        tags = _RENDER_PAGE_TAGS
        examples = [
            FunctionExample(
                sql="SELECT pdf.render_page('not-a-pdf'::BLOB, 1)",
                description="Render the first page of PDF bytes to a PNG (NULL for non-PDF bytes)",
            ),
        ]

    @classmethod
    def compute(
        cls,
        pdf: Annotated[pa.BinaryArray, Param(doc="Raw PDF bytes.", arrow_type=pa.binary())],
        page: Annotated[pa.Int32Array, Param(doc="1-based page number.", arrow_type=pa.int32())],
    ) -> Annotated[pa.BinaryArray, Returns(arrow_type=pa.binary())]:
        """Compute the output column for a batch of input rows."""
        return _render(_sources_from_bytes(pdf), page, None)


class RenderPageBytesDpiFunction(ScalarFunction):
    """``render_page(blob, page, dpi)`` -- PNG of one page (bytes) at a DPI."""

    class Meta:
        """Function metadata (name, description, examples)."""

        name = "render_page"
        description = "Render one page of a PDF (BLOB bytes) to a PNG BLOB at a given DPI (capped at 300)"
        categories = ["pdf", "render"]
        tags = _RENDER_PAGE_TAGS
        examples = [
            FunctionExample(
                sql="SELECT pdf.render_page('not-a-pdf'::BLOB, 1, 72)",
                description="Render the first page of PDF bytes at 72 DPI (NULL for non-PDF bytes)",
            ),
        ]

    @classmethod
    def compute(
        cls,
        pdf: Annotated[pa.BinaryArray, Param(doc="Raw PDF bytes.", arrow_type=pa.binary())],
        page: Annotated[pa.Int32Array, Param(doc="1-based page number.", arrow_type=pa.int32())],
        dpi: Annotated[int, ConstParam("Render resolution in DPI (capped at 300).", arrow_type=pa.int32())],
    ) -> Annotated[pa.BinaryArray, Returns(arrow_type=pa.binary())]:
        """Compute the output column for a batch of input rows."""
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
