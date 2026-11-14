# PDF SQL notebooks

[A printed total can balance while a line is wrong](01_extraction_and_invoice_arithmetic.ipynb) extracts a known PDF table, casts invoice fields to exact SQL decimal values, locates item words on a worker-rendered page, and compares printed amounts with quantity × unit price.

## Run from this directory

Install [uv](https://docs.astral.sh/uv/), Git, and Python 3.13 or 3.14. First use needs network access for dependencies, published Haybarn/VGI and the pinned worker revision. A copy of this entire directory runs without the worker source checkout or a local worker build.

```bash
uv sync --frozen
uv run --frozen python execute.py
```

The visible SQL launches `uvx --python 3.13 --with vgi-python==0.51.0 --with vgi-rpc==0.53.1 --with pdfplumber==0.11.10 --with pypdfium2==5.15.0 --with pikepdf==10.17.0 --with Pillow==13.0.0 --from git+https://github.com/Query-farm/vgi-pdf@20e1590922c3ec394e53141b6abaffbb7ff4d7b9 vgi-pdf-worker`. These PDF-library versions match the worker's tested lock. The separately locked notebook environment uses Haybarn 2.0.0rc11. Changing the worker/library pins should be followed by default and page-two validation.

For interactive use, register the notebook interpreter and select it in Jupyter:

```bash
uv run --frozen python -m ipykernel install --user --name vgi-pdf-notebooks --display-name 'Python (vgi-pdf notebooks)'
```

Restart and run all after editing `PAGE_NUMBER`. Python setup, plotting, interpretation source and cleanup are folded; connection SQL, extraction/transformation SQL and outputs remain visible. The runner creates a temporary kernel, executes with this directory as cwd and saves only after success. Overrides require a separate output directory; unknown parameters and a destination equal to the source are rejected.

## Focused experiment and fixture

```bash
uv run --frozen python execute.py --parameters '{"PAGE_NUMBER":2}' --output-dir validation/page-two
```

The default is page 1. Page 2 deliberately changes TAPE quantity from 10 to 12 while leaving its printed amount and the printed total stale. The PDF is explicitly synthetic and carries no real transaction or customer data. [Provenance](fixtures/provenance.json) records its SHA-256, generator, intended variants, geometry and currency semantics. The invariant PDF metadata clock is deterministic, not an observed creation or transaction time.

The committed PDF is sufficient to execute. To regenerate its deterministic bytes with the locked ReportLab dependency:

```bash
uv run --frozen python generate_fixture.py
```

ReportLab creates fixture input only. The notebook obtains page count, page geometry, table cells, word boxes and rendered PNG bytes exclusively from the real VGI worker. It uses no separate Python extraction path.

## Checks and limits

Twelve SQL invariants cover page/cell identity, header mapping, numeric parsing, unique item-word matches, word-box geometry, printed-total accounting and exact discrepancy arithmetic. A separate check verifies the full PNG dimensions at 144 DPI. The layout chart displays a content crop in PDF points, mapping the full raster back to page coordinates; boxes mark SKU words, not entire rows. The scalar render and structure calls are materialized before plotting.

The intended table has four named headers, unrotated US Letter pages, unique SKU words, integer quantities and two-decimal USD prices without tax, discount or shipping. Header-to-column mapping and row parsing are specific to this fixture. The notebook does not implement general invoice parsing, OCR, merged-cell recovery, duplicate-label disambiguation or business rules beyond the displayed formula. A correctly extracted number can still be inconsistent with the document's other numbers.

Saved default outputs are embedded in the notebook. Generated logs, review charts and experiment copies are ignored under `validation/`. The runner copies fixtures, branding and runtime companions alongside executed experiment copies. [STANDARD.md](STANDARD.md) records presentation conventions.
