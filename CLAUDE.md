# CLAUDE.md — vgi-pdf

Contributor/agent notes. User-facing docs live in `README.md`; this is the
"how it's built and where the sharp edges are" companion.

## What this is

A [VGI](https://query.farm) worker that extracts **PDF structure** — tables,
word bounding boxes, page geometry, form fields, metadata, encryption status —
and **renders pages to PNG**, as DuckDB functions. **Not** `vgi-tika`: tika does
plain text; vgi-pdf does layout / tables / coordinates / rendering. `pdf_worker.py`
assembles every function into one `pdf` catalog (single `main` schema) over
stdio. Sibling style/tooling to `vgi-conform` / `vgi-calendar`.

Backed by permissive libraries only — `pdfplumber` (MIT), `pypdfium2`
(Apache-2.0), `pikepdf` (MPL-2.0), `Pillow` (MIT). **Never PyMuPDF/`fitz`**
(AGPL/commercial — see README "Why not PyMuPDF").

## Layout

```
pdf_worker.py          repo-root stdio entry point; PEP 723 inline deps; main()
vgi_pdf/
  core.py              pure PDF parse/render logic; no Arrow/VGI; unit-testable; hostile-input safe
  scalars.py           per-row scalars (path/bytes input-type overloads; render_page dpi overloads)
  tables.py            table functions: tables, words, pages (path + bytes overloads)
  schema_utils.py      pa.Field comment / column-doc helper
tests/                 pytest: fixtures (in-test PDF gen), test_core (pure), test_tables (in-proc), test_scalars (Client RPC)
test/sql/*.test        haybarn-unittest sqllogictest — authoritative E2E
test/sql/data/         tiny committed fixtures (table.pdf, form.pdf, meta.pdf, garbage.pdf)
Makefile               test / test-unit / test-sql / lint
```

To add a function: implement the logic in `core.py` (pure, **total** — never
raises on garbage for scalars; returns `None`/empty), wrap it as a scalar or
table function in the matching module, register it in `pdf_worker.py`'s
`_FUNCTIONS`.

## Scalars vs table functions — THE core convention (read first)

The VGI SDK makes **scalar functions positional-only**: `name := value` named
args are rejected for scalars and only work on table functions. This drove the
function-shape split:

- **Per-row functions are scalars** (`page_count`, `is_encrypted`,
  `pdf_metadata`, `form_fields`, `render_page`) so they work inline in a
  projection.
- **Structure (many rows per PDF) are table functions** (`tables`, `words`,
  `pages`), which take the optional `page :=` filter.

## Table-function scan state = the HTTP-continuation cursor (READ THIS)

The structure table functions (`tables`, `words`, `pages`) emit **many rows per
PDF** — `words` is hundreds–thousands per page, `tables` one row per cell — so
the output routinely exceeds a single producer batch. That makes the
externalized **scan cursor** load-bearing, not optional.

Over the **stateless HTTP transport** the framework wire-serializes a producer's
per-scan state after every `process()` tick (`ArrowSerializableDataclass.
serialize_to_bytes()`), the client returns the continuation token, and the worker
resumes by deserializing it — emitting at most **one producer batch per HTTP
response**. A position-less `state: None` generator that did
`out.emit(...ALL rows...); out.finish()` would restart from row 0 on **every**
HTTP resume and **loop forever** once the output exceeds one batch.
subprocess/unix keep the live state in-process so they hide the bug; **only the
http leg (and the serialize-between-ticks unit test) expose it.**

Fix (in `tables.py`, mirrors vgi-search's `ScanState`): every function is a
`TableFunctionGenerator[Args, ScanState]` with `initial_state() -> ScanState()`.
`ScanState(ArrowSerializableDataclass)` carries `started: bool`, `offset: int`,
`rows_ipc: bytes` — all plainly serializable, so they survive the continuation
token. On the **first** tick `process()` reads the PDF, materializes the **full**
result batch into `rows_ipc` (via `result_to_ipc`), and sets `started`; each tick
then emits a **bounded `ROWS_PER_TICK`-row slice** from `offset`, advances
`offset`, and `out.finish()`es once drained (an empty/NULL source materializes 0
rows and finishes immediately: `0 >= 0`). The `_build_*` helpers return the full
RecordBatch; `_stream_slice` does the cursor slicing. **The NULL/empty-source
early `out.finish()` paths stay.** Rows/schema are byte-identical to the old
emit-all path.

Regression guard: `tests/harness.invoke_table_function(..., serialize_state=True)`
round-trips the state through `serialize_to_bytes`/`deserialize_from_bytes`
between every tick (1000-tick guard). `TestScanStateRoundTrip` /
`TestCursorSurvivesContinuation` in `test_tables.py` assert identical rows/order,
no dupes, termination, and bounded chunks (`>= 2` batches each `<= ROWS_PER_TICK`
— this is the **fail-old** assertion; old code emits exactly one batch). The
`structure.test` SQL case pages `manywords.pdf` (200 words > `ROWS_PER_TICK`) and
asserts `count(*) = 200` + an ordered head — over http that only terminates if
the cursor works. NOTE: a table-function arg can't be a `(SELECT ...)` subquery in
`.test`, so `tables`/`words` are driven via the **VARCHAR path overload** there.

## The polymorphic `pdf` input (path OR bytes) — overloads, not AnyArrow

Every function accepts the PDF as a **`VARCHAR` path** *or* a **`BLOB` of
bytes**. These are two distinct DuckDB signatures. Rather than an `AnyArrow`
param that inspects the runtime type, each function is registered **twice** —
a `*PathFunction` (input typed `pa.string()`) and a `*BytesFunction` (input
typed `pa.binary()`) — sharing one `Meta.name`. This is the same explicit-
overload idiom `vgi-conform` uses for optional args, and it lets DuckDB dispatch
on the column type. (Don't build the overload classes from a factory: a nested
`class Meta:` body can't close over an enclosing variable — write them out.)

`render_page` multiplies this by the optional `dpi`: 4 overloads
(`(path,page)`, `(path,page,dpi)`, `(blob,page)`, `(blob,page,dpi)`).

`PdfSource` (in `core.py`) is the normalized handle: `from_path` / `from_bytes`
build one (or `None` for a NULL input), and `read_bytes()` lazily reads the file
or returns the in-memory bytes. Only local files / given bytes are ever touched.

## Sharp edges (learned the hard way)

1. **`haybarn-unittest` skips `require vgi`.** Use an explicit `statement ok` /
   `LOAD vgi;` instead (every `.test` here does). `require-env VGI_PDF_WORKER`
   gates the suite on the worker being configured.
2. **NULL vs unreadable vs error — scalars never raise.** NULL `pdf` → NULL.
   A malformed/encrypted/unreadable PDF → NULL for scalars (the worker must
   survive hostile input). Table functions raise `core.PdfError` on an
   unopenable PDF so DuckDB shows a clean error instead of the worker dying.
3. **`is_encrypted` treats encryption as a *success*, not a failure.** pikepdf's
   `Pdf.open` raises `PasswordError` for an encrypted-no-password file; we catch
   that and return `true` (definitively encrypted), only returning `None` when
   the bytes aren't a readable PDF at all. Never hangs / brute-forces.
4. **LIST/MAP returns need explicit `Returns(arrow_type=...)`.** `pdf_metadata`
   and `form_fields` return `MAP(VARCHAR,VARCHAR)`; the SDK raises without
   `Returns(arrow_type=pa.map_(pa.string(), pa.string()))`. MAP values are
   emitted as a list of `(key, value)` tuples per row.
5. **`row` is a SQL keyword.** The `tables` output field is named `row`; quote
   it (`"row"`) in SQL. We kept the name (the spec lists it) rather than
   renaming to `row_index`, and documented the quoting in the schema comment.
6. **`render_page` is bounded.** DPI clamps to `MAX_RENDER_DPI=300` (default
   `DEFAULT_DPI=150`); the output bitmap area is capped at
   `MAX_RENDER_PIXELS=25M` — an enormous page is rendered at reduced scale, not
   allowed to OOM. PNG magic bytes (`\x89PNG`) are asserted in tests.
7. **Heavy imports are module-level.** `pdfplumber`, `pikepdf`, `pypdfium2` are
   imported once at `core.py` load and reused for the process lifetime — never
   per row.
8. **The unit suite can pass while the RPC path is broken.** `test_core.py`
   calls pure functions; only `test_scalars.py` (real `vgi.client.Client`
   subprocess) and `test/sql/*.test` (real `ATTACH`+`SELECT`) exercise the wire.
   **Run the SQL suite** — it's authoritative.

## Why not PyMuPDF (licensing — the load-bearing decision)

PyMuPDF / `fitz` is **AGPL-3.0 or paid commercial** — its network-copyleft is
unacceptable for a commercial marketplace worker. We use only permissive libs
(pdfplumber MIT, pypdfium2 Apache-2.0/BSD-3, pikepdf MPL-2.0, Pillow MIT). See
the README table. If you're tempted to reach for `fitz` for a feature, **don't**
— find the permissive equivalent or leave the feature out.

## Testing

```sh
uv run pytest -q              # unit: pure core + in-proc tables + Client RPC scalars
make test-sql                 # E2E: haybarn-unittest over test/sql/*  (authoritative)
make test                     # both
uv run ruff check . && uv run mypy vgi_pdf/
```

`make test-sql` sets `VGI_PDF_WORKER="uv run --python 3.13 pdf_worker.py"`, puts
`~/.local/bin` on PATH, and runs `haybarn-unittest --test-dir . "test/sql/*"`.
Install the runner once with `uv tool install haybarn-unittest`. CI
(`.github/workflows/ci.yml`) runs unit + lint + a gated `e2e` job.

Test PDFs are generated deterministically in-test with `reportlab` (a dev-only
dep); a few tiny fixtures are committed under `test/sql/data/` for the SQL E2E
suite. Everything is offline/hermetic. If `make test-sql` flakes, re-run 2–3× —
only a consistent failure is real.
