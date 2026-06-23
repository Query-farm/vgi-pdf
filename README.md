<p align="center">
  <img src="docs/vgi-logo.png" alt="Vector Gateway Interface (VGI)" width="320">
</p>

<p align="center"><em>A <a href="https://query.farm">Query.Farm</a> VGI worker for DuckDB.</em></p>

# vgi-pdf

[![CI](https://github.com/Query-farm/vgi-pdf/actions/workflows/ci.yml/badge.svg)](https://github.com/Query-farm/vgi-pdf/actions/workflows/ci.yml)

A [VGI](https://query.farm) worker that extracts **PDF structure** into
DuckDB/SQL — **tables, word bounding boxes, page geometry, form fields,
metadata, encryption status**, and **page rendering** — as plain SQL functions.

This is deliberately **not** [`vgi-tika`](https://github.com/Query-farm/vgi-tika):
*tika does plain text; vgi-pdf does layout / tables / coordinates / rendering.*
It is backed by permissive, commercially-safe libraries:
[`pdfplumber`](https://pypi.org/project/pdfplumber/) (MIT, on `pdfminer.six`
MIT), [`pypdfium2`](https://pypi.org/project/pypdfium2/) (Apache-2.0 / BSD-3,
Google PDFium), and [`pikepdf`](https://pypi.org/project/pikepdf/) (MPL-2.0).

```sql
INSTALL vgi FROM community; LOAD vgi;
ATTACH 'pdf' (TYPE vgi, LOCATION 'uv run pdf_worker.py');

-- Scalars: one PDF in, one value out
SELECT pdf.page_count('report.pdf');                 -- 3
SELECT pdf.is_encrypted('report.pdf');               -- false
SELECT pdf.pdf_metadata('report.pdf')['Title'];      -- 'Quarterly Report'
SELECT pdf.form_fields('application.pdf');           -- MAP{'full_name': 'Ada'}
SELECT pdf.render_page('report.pdf', 1);             -- PNG BLOB (default 150 DPI)
SELECT pdf.render_page('report.pdf', 1, 72);         -- PNG BLOB at 72 DPI

-- Table functions: many rows per PDF
SELECT * FROM pdf.tables('report.pdf') ORDER BY page, table_index, row, col;
SELECT * FROM pdf.words('report.pdf') ORDER BY page, top, x0;
SELECT * FROM pdf.pages('report.pdf');
```

Every function accepts the PDF as **either** a `VARCHAR` filesystem path the
worker opens **or** a `BLOB` of the raw PDF bytes — so you can work over files on
disk or over a `BLOB` column you already loaded:

```sql
SELECT pdf.page_count(content) FROM read_blob('*.pdf');       -- bytes
SELECT pdf.page_count(filename) FROM glob('*.pdf') t(filename); -- path
```

## Scalars (per-row) vs. table functions (structure)

The split follows what the VGI SDK allows for each function shape:

* **Scalars** take **positional** arguments only and resolve overloads by the
  *types* of those arguments (DuckDB's `name := value` syntax is a
  table-function feature, not a scalar one). The polymorphic `pdf` input — a
  `VARCHAR` path or a `BLOB` — is two distinct DuckDB signatures, so each scalar
  is registered for both. `render_page`'s optional `dpi` (default 150) is an
  extra arity overload: `render_page(pdf, page)` / `render_page(pdf, page, dpi)`.

* **Table functions** return *many* rows per PDF (the structure itself) and
  accept DuckDB's `name := value` syntax for the optional `page` filter:

  ```sql
  SELECT * FROM pdf.tables('report.pdf', page := 1);   -- only page 1's cells
  SELECT * FROM pdf.words('report.pdf', page := 2);    -- only page 2's words
  ```

## Function catalog

| Function | Form | Signature | Returns |
| --- | --- | --- | --- |
| `page_count` | scalar | `(pdf)` | `INTEGER` (NULL if unreadable) |
| `is_encrypted` | scalar | `(pdf)` | `BOOLEAN` (NULL if unreadable) |
| `pdf_metadata` | scalar | `(pdf)` | `MAP(VARCHAR, VARCHAR)` (Title/Author/Producer/…) |
| `form_fields` | scalar | `(pdf)` | `MAP(VARCHAR, VARCHAR)` (AcroForm name→value) |
| `render_page` | scalar | `(pdf, page[, dpi])` | `BLOB` (PNG; NULL on failure) |
| `tables` | table | `(pdf, page := NULL)` | `(page, table_index, row, col, value)` |
| `words` | table | `(pdf, page := NULL)` | `(page, text, x0, top, x1, bottom)` |
| `pages` | table | `(pdf)` | `(page, width, height, rotation)` |

`pdf` is a `VARCHAR` path **or** a `BLOB` of PDF bytes in every function. Pages
are **1-based**. Coordinates (`x0/top/x1/bottom`, `width/height`) are in PDF
points; `top`/`bottom` are measured from the top of the page (pdfplumber
convention). `tables` is **long format** — one row per cell — so it drops
straight into SQL; `row` is a SQL keyword, so quote it (`"row"`) when you select
it.

## NULL & hostile-input semantics

A NULL `pdf` input yields NULL output (scalars) or no rows (table functions).
**PDFs are hostile input**, so the worker is built to survive anything:

* A **malformed / truncated / non-PDF** input → scalars return NULL; table
  functions raise a clean DuckDB error (the worker never crashes).
* An **encrypted PDF with no password** → `is_encrypted` returns `true`
  (detecting encryption is the point), other scalars return NULL, and rendering
  / extraction return NULL / a clean error — **never a hang**.
* A **"bomb" PDF** (huge pages, pathological structure) can't exhaust memory:
  rendering is bounded (see below).

## Why not PyMuPDF? (licensing — read this)

The obvious library for this job is **PyMuPDF (`fitz`)**, and we deliberately do
**not** use it. PyMuPDF is licensed **AGPL-3.0 or a paid commercial license**.
The AGPL's network-copyleft is unacceptable for a commercial data marketplace:
shipping it in a hosted service would oblige us (and downstream users) to offer
the complete corresponding source of the surrounding service under the AGPL, or
to buy commercial licenses. That is a non-starter for `vgi-pdf`'s own MIT code.

Instead `vgi-pdf` uses only **permissive** libraries, none of which impose
copyleft on this project or its users:

| Concern | Library | License |
| --- | --- | --- |
| words + boxes, tables, geometry | `pdfplumber` (on `pdfminer.six`) | **MIT** |
| page rendering to PNG | `pypdfium2` (Google PDFium) | **Apache-2.0 / BSD-3** |
| metadata, AcroForm fields, encryption | `pikepdf` (on QPDF) | **MPL-2.0** |
| PNG encode | `Pillow` | **MIT-CMU (HPND)** |

`pikepdf` is MPL-2.0 (weak, file-level copyleft): we use it as an unmodified,
separately pip-installed dependency, which keeps `vgi-pdf`'s own code under MIT
and fine for commercial use. None of these libraries is AGPL.

## Threat model & resource bounds

`vgi-pdf` treats every input as adversarial:

* **No external resource resolution.** Only the bytes you hand it (or the local
  file path you hand it) are read. The worker never follows URLs, remote
  XObjects, JavaScript, or embedded references.
* **Bounded rendering.** `render_page` clamps DPI to **300** (`MAX_RENDER_DPI`),
  defaults to **150** (`DEFAULT_DPI`), and caps the rasterised bitmap area at
  **25M pixels** (`MAX_RENDER_PIXELS`) — an over-large page is rendered at a
  reduced scale rather than allowed to exhaust memory.
* **Total functions.** Every parse/render is wrapped per row in `try/except`.
  Scalars map any failure to NULL; table functions surface a clean error. A
  single bad row never aborts a batch or crashes the worker process.
* **Encrypted PDFs never hang.** An encrypted PDF opened without a password is
  detected (`is_encrypted` → `true`) and otherwise degrades to NULL / error
  immediately — there is no password brute-forcing and no blocking.

## Testing

```sh
uv sync --extra dev
uv run pytest -q              # unit: pure core logic + in-proc tables + Client RPC scalars
make test-sql                 # E2E: haybarn-unittest over test/sql/*  (authoritative)
make test                     # both
uv run ruff check . && uv run mypy vgi_pdf/
```

Test PDFs are generated deterministically in-test (via `reportlab`), and a few
tiny fixtures are committed under `test/sql/data/` for the SQL E2E suite:

* `table.pdf` — a known 2×2 table plus known words;
* `form.pdf` — a single AcroForm text field with a known value;
* `meta.pdf` — a document with a known `Title`;
* `garbage.pdf` — not a PDF at all (hostile-input survival case).

Everything is offline and hermetic (no network), so the suite is fast and
deterministic.

## License

MIT — see [`LICENSE`](LICENSE). The third-party PDF libraries keep their own
permissive licenses (MIT / Apache-2.0 / BSD-3 / MPL-2.0), as noted above.

---

## Authorship & License

Written by [Query.Farm](https://query.farm) — every VGI worker is designed and built by Query.Farm.

Copyright 2026 Query Farm LLC - https://query.farm

