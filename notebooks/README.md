# PDF Structure & Rendering examples

Two executed SQL stories use public worker release **v0.4.0**:

- [A printed total can balance while a line is wrong](01_extraction_and_invoice_arithmetic.ipynb) extracts a known ruled table, casts exact SQL decimals, locates SKU words on the worker-rendered page and checks line arithmetic. Change `PAGE_NUMBER` from 1 to 2.
- [No form fields is different from an unreadable document](02_empty_fields_and_unavailable_documents.ipynb) preserves empty maps, NULL extraction and encryption in a six-input inventory before applying a narrow review rule. Change `REQUIRED_FIELD` from `full_name` to `email`.

Every document is explicitly synthetic. No real transactions, customer documents or credentials are included. [Invoice provenance](fixtures/provenance.json) and [intake provenance](fixtures/intake-provenance.json) record the exact bytes and their origins. Metadata authors/timestamps are fixture claims, not authenticated authorship or acquisition records.

## Run from this folder or a single notebook

Install [uv](https://docs.astral.sh/uv/) and Git, then:

```bash
uv sync --locked
uv run --locked vgi-notebooks 01_extraction_and_invoice_arithmetic.ipynb
uv run --locked vgi-notebooks 02_empty_fields_and_unavailable_documents.ipynb
```

The local environment pins shared **v0.3.35** with `[runner]`. A Colab badge opens one notebook; its conditional setup installs only the shared base package in that kernel. `prepare("pdf")` downloads eight companion files from immutable public commit `81b424790808028d67de14da45c57b5ab7e7f622` and verifies their SHA-256 hashes. No parent source checkout, root virtual environment or worker build is used.

The visible SQL uses a short released launcher:

```sql
INSTALL vgi FROM community;
LOAD vgi;
ATTACH 'pdf' (TYPE vgi, LOCATION 'uvx --python 3.13 --from git+https://github.com/Query-farm/vgi-pdf@v0.4.0 vgi-pdf-worker');
```

Release v0.4.0 resolves to `d27c04f46630f4a7202141a8fa6a99734c18e5e2`. uv installs the worker's declared dependencies in a separate Python 3.13 environment; no transitive override stack is needed. The notebook uses published Haybarn 2.0.0rc11. First use needs GitHub, package indexes and extension retrieval. Analysis uses local files/bytes only. Shared connection setup creates an empty temporary SQL-secret directory and disables persistent secrets before ATTACH; cleanup checks the inventory and closes the connection.

For an editor, select a registered notebook interpreter:

```bash
uv run --locked python -m ipykernel install --user --name pdf-examples \
  --display-name "Python (vgi-pdf notebooks)"
```

Restart and Run All after editing parameters. Helper/setup code is folded; ATTACH, extraction and analytical SQL remain visible. Figures use SVG and matching GitHub image links. The page-layout figure embeds the **worker-rendered raster PNG** within SVG axes and vector bounding-box annotations; it is not a vector reconstruction of PDF page content. Python draws the returned image/numbers and checks its dimensions; it does not extract PDF text, tables or fields.

## Separate experiments

```bash
uv run --locked vgi-notebooks 01_extraction_and_invoice_arithmetic.ipynb \
  --parameters '{"PAGE_NUMBER":2}' --output-dir artifacts/page-two
uv run --locked vgi-notebooks 02_empty_fields_and_unavailable_documents.ipynb \
  --parameters '{"REQUIRED_FIELD":"email"}' --output-dir artifacts/email-required
```

Overrides require a separate output directory and preserve saved defaults. Experiment copies retain branding/documentation and immutable companions. Reset defaults to 1 and `full_name` respectively.

In the invoice, page 2 changes TAPE quantity from 10 to 12 while leaving its printed amount and total stale. The printed lines still sum to USD315.00, but quantity × unit price sums to USD340.00. This is extraction and arithmetic consistency for one known layout, not a determination of what anyone owes. The parser assumes four known headers, unique SKU words, unrotated Letter pages, exact two-decimal USD values and no tax/discount/shipping adjustments. It is not a general invoice parser or OCR system.

In the intake, the filled form has a valid empty metadata map, while the metadata-only PDF and invoice have valid empty form maps. Encryption is positively detected in a deliberately protected input; malformed/missing inputs have unknown encryption and unavailable maps. These cases are never collapsed to zero. Requiring `email` instead of `full_name` removes the sole ready-for-review candidate while keeping all six input rows and five route categories visible. The rule checks only one top-level machine-readable AcroForm value; it does not verify identity, validate email syntax, infer printed fields or authenticate metadata claims. The notebook never decrypts the protected input.

## Fixture generation and catalog scope

The committed files are sufficient to run. `generate_fixture.py` documents the original deterministic invoice generation with ReportLab5.1.0; optional regeneration requires that package explicitly:

```bash
uv run --with reportlab==5.1.0 python generate_fixture.py
```

The released `form.pdf`, `meta.pdf` and malformed `garbage.pdf` are unchanged copies. `generate_intake_fixture.py` records how the encrypted control was derived from `meta.pdf` using pikepdf in the released worker environment. Its public demonstration password protects no sensitive data. Encryption uses fresh random material, so regeneration can change bytes; update the provenance hash and repeat validation before replacing a committed fixture. Normal notebook runs do not regenerate files. PDF fixtures are marked binary to preserve exact bytes and cross-reference offsets.

The actual runtime catalog has **nine named surfaces**: eight functions represented by fifteen overload rows, plus the `functions` discovery view. Both stories together cover **9/9**, including per-row metadata/form/encryption behavior and a registry join that verifies the planned intake operations are scalars. Full catalogs, argument metadata and structure/view column signatures are in [coverage/](coverage/); [coverage.json](coverage.json) links successful executed SQL to each narrative and result.

Current public revision `35f44ffdbb99e284b0561ebeef7a93b5966f5214` adds metadata examples and fixture-SQL helpers used by those examples; PDF processing and root dependencies are unchanged. Actual release/current signatures match. Saved notebooks run the release, so current-interface coverage is compatible evidence, not a claim of separate current-main notebook execution. Coverage counts names once and does not claim every overload or every malformed input was separately exercised.

Validation runs both defaults and detached one-parameter experiments, 12+14 SQL checks, render-dimension checks, six overwrite/parameter guards, a fresh notebook-only compatible Python3.12 kernel with eight libraries preloaded, output/figure review and coverage validation. Hosted Colab itself is not manually operated. Worker source is unchanged and [successful source CI](https://github.com/Query-farm/vgi-pdf/actions/runs/43577675136) is reused; no engine build, repeated source suite or deployment is performed. See the [shared standard](STANDARD.md).
