"""Deterministic, in-test PDF generators for the vgi-pdf suite.

Everything is built from ``reportlab`` (dev-only dep) and ``pikepdf`` so tests
never depend on committed nondeterministic blobs. The same builders also write
the tiny committed fixtures under ``test/sql/data/`` (see ``regenerate_sql_fixtures``).

The geometry is fixed (US Letter, 612x792 pt) so word-box and table-cell
assertions are stable across runs.
"""

from __future__ import annotations

import io
import os

import pikepdf
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

# Make reportlab + pikepdf output byte-deterministic: fix the embedded
# timestamps (SOURCE_DATE_EPOCH) and pass ``invariant=True`` to the canvas so
# the committed SQL fixtures don't churn on regeneration.
os.environ.setdefault("SOURCE_DATE_EPOCH", "1700000000")


def _canvas(buf: io.BytesIO) -> canvas.Canvas:
    return canvas.Canvas(buf, pagesize=letter, invariant=True)


# Known content the tests assert against.
TABLE_CELLS = [["Name", "Age"], ["Ada", "36"]]
KNOWN_WORDS = ["Hello", "World", "Structured", "Layout"]
KNOWN_TITLE = "Quarterly Report"
KNOWN_AUTHOR = "Ada Lovelace"
FORM_FIELD_NAME = "full_name"
FORM_FIELD_VALUE = "Ada Lovelace"


def make_words_pdf() -> bytes:
    """A single page with four known words at known positions."""
    buf = io.BytesIO()
    c = _canvas(buf)
    c.setTitle(KNOWN_TITLE)
    c.setAuthor(KNOWN_AUTHOR)
    # Place words at increasing y (drawn from the bottom in PDF coords).
    c.drawString(72, 720, "Hello World")
    c.drawString(72, 690, "Structured Layout")
    c.showPage()
    c.save()
    return buf.getvalue()


def make_table_pdf() -> bytes:
    """A single page with a ruled 2x2 table plus the known words.

    The table is drawn as explicit lines + cell text so pdfplumber's
    line-based detector reliably finds a 2x2 grid.
    """
    buf = io.BytesIO()
    c = _canvas(buf)
    c.setTitle(KNOWN_TITLE)
    c.setAuthor(KNOWN_AUTHOR)

    # Words (so the same fixture exercises words + tables).
    c.drawString(72, 740, "Hello World")
    c.drawString(72, 720, "Structured Layout")

    # 2x2 grid: columns at x=100,250,400 ; rows at y=600,560,520 (top-down).
    xs = [100, 250, 400]
    ys = [600, 560, 520]
    c.setLineWidth(1)
    for x in xs:
        c.line(x, ys[-1], x, ys[0])
    for y in ys:
        c.line(xs[0], y, xs[-1], y)

    # Cell text, centered-ish within each cell.
    cells = TABLE_CELLS
    for r, row in enumerate(cells):
        for col, val in enumerate(row):
            cx = xs[col] + 10
            # row 0 is the top band (between ys[0] and ys[1]).
            cy = ys[r] - 25
            c.drawString(cx, cy, val)
    c.showPage()
    c.save()
    return buf.getvalue()


def make_multipage_pdf() -> bytes:
    """Three pages, so page-count / per-page filters are exercised."""
    buf = io.BytesIO()
    c = _canvas(buf)
    c.setTitle(KNOWN_TITLE)
    for i in range(1, 4):
        c.drawString(72, 720, f"Page {i} content")
        c.showPage()
    c.save()
    return buf.getvalue()


def make_meta_pdf() -> bytes:
    """A document whose metadata carries a known Title and Author."""
    return make_words_pdf()


def make_form_pdf() -> bytes:
    """A PDF with a single AcroForm text field with a known value.

    Built by hand with pikepdf so the AcroForm + field-value are unambiguous and
    pikepdf can read ``/V`` back.
    """
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))

    field = pikepdf.Dictionary(
        FT=pikepdf.Name("/Tx"),
        T=pikepdf.String(FORM_FIELD_NAME),
        V=pikepdf.String(FORM_FIELD_VALUE),
        Subtype=pikepdf.Name("/Widget"),
        Rect=pikepdf.Array([100, 700, 300, 720]),
        F=4,
    )
    field_ref = pdf.make_indirect(field)
    page.Annots = pikepdf.Array([field_ref])

    pdf.Root.AcroForm = pikepdf.Dictionary(
        Fields=pikepdf.Array([field_ref]),
        NeedAppearances=True,
    )

    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def make_encrypted_pdf(password: str = "secret") -> bytes:
    """A valid PDF encrypted with a user password (open requires the password)."""
    base = make_words_pdf()
    pdf = pikepdf.open(io.BytesIO(base))
    buf = io.BytesIO()
    pdf.save(buf, encryption=pikepdf.Encryption(user=password, owner=password))
    return buf.getvalue()


def make_garbage_bytes() -> bytes:
    """Not a PDF at all — the hostile-input survival case."""
    return b"this is definitely not a pdf \x00\x01\x02 %%%% garbage"


def regenerate_sql_fixtures(data_dir: str) -> None:
    """Write the committed SQL E2E fixtures into ``data_dir``."""
    os.makedirs(data_dir, exist_ok=True)
    # NOTE: encrypted.pdf is deliberately NOT committed — pikepdf encryption
    # embeds a random document ID/salt, so the bytes aren't reproducible. The
    # encrypted-PDF behaviour is covered by the unit suite (generated in-test);
    # the SQL E2E hostile case uses the deterministic garbage.pdf instead.
    files = {
        "table.pdf": make_table_pdf(),
        "words.pdf": make_words_pdf(),
        "multipage.pdf": make_multipage_pdf(),
        "meta.pdf": make_meta_pdf(),
        "form.pdf": make_form_pdf(),
        "garbage.pdf": make_garbage_bytes(),
    }
    for name, data in files.items():
        with open(os.path.join(data_dir, name), "wb") as fh:
            fh.write(data)


if __name__ == "__main__":
    import pathlib

    here = pathlib.Path(__file__).resolve().parent.parent
    regenerate_sql_fixtures(str(here / "test" / "sql" / "data"))
    print("wrote SQL fixtures to test/sql/data/")
