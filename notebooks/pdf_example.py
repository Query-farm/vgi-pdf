"""Validate authored inputs and draw SQL-returned PDF results; never extract PDF text."""

import hashlib
import io
import json
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.ticker import MaxNLocator
from PIL import Image
from vgi_notebooks import finish


def validate_page(value):
    """Limit invoice selection to the two authored pages."""
    if type(value) is not int or value not in {1, 2}:
        raise ValueError("PAGE_NUMBER must be integer 1 or 2")


def validate_field(value):
    """Limit the review experiment to the two documented field names."""
    if value not in {"full_name", "email"}:
        raise ValueError("REQUIRED_FIELD must be full_name or email")


def verify_fixtures():
    """Verify every committed input before any worker call."""
    invoice = json.loads(Path("fixtures/provenance.json").read_text())
    if hashlib.sha256((Path("fixtures") / invoice["file"]).read_bytes()).hexdigest() != invoice["sha256"]:
        raise ValueError("Invoice fixture differs from recorded bytes")
    for item in json.loads(Path("fixtures/intake-provenance.json").read_text())["files"]:
        data = (Path("fixtures") / item["file"]).read_bytes()
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("Intake fixture differs from recorded bytes")


def invoice_layout(raster, geometry, boxes, PAGE_NUMBER):
    """Draw the worker render and SQL-returned SKU boxes in PDF points."""
    assert raster.png.iloc[0] is not None, "Worker could not render the selected PDF page"
    page_image = Image.open(io.BytesIO(bytes(raster.png.iloc[0])))
    width = float(geometry.width.iloc[0])
    height = float(geometry.height.iloc[0])
    assert page_image.size == (round(width * 2), round(height * 2)), page_image.size
    fig, ax = plt.subplots(figsize=(11, 8))
    ax.imshow(page_image, extent=(0, width, height, 0))
    for row in boxes.itertuples():
        color = "#b25b3f" if row.discrepancy else "#397d62"
        ax.add_patch(
            Rectangle(
                (row.x0 - 2, row.top - 2),
                row.x1 - row.x0 + 4,
                row.bottom - row.top + 4,
                fill=False,
                edgecolor=color,
                linewidth=1.8,
            )
        )
    ax.set_xlim(40, 575)
    ax.set_ylim(450, 40)
    ax.grid(False)
    ax.set_xlabel("Horizontal position · PDF points from left")
    ax.set_ylabel("Vertical position · PDF points from top")
    finish(
        fig,
        "Check the extracted keys against visible document layout",
        f"Synthetic page {PAGE_NUMBER} · 144-DPI crop · SKU boxes: green agrees, rust flags arithmetic",
        "Source: authored two-page invoice fixture · invented USD values, not a real transaction\n"
        "VGI PDF v0.4.0 extraction/rendering + visible SQL; no OCR or general invoice parsing",
        name="01-invoice-layout",
    )


def invoice_amounts(lines, PAGE_NUMBER):
    """Compare printed and computed values returned by exact SQL arithmetic."""
    fig, ax = plt.subplots(figsize=(11, 5.6))
    y = list(range(len(lines)))
    for column, offset, color, label in [
        ("printed_amount", -0.18, "#a5bfb6", "Printed line amount"),
        ("computed_amount", 0.18, "#397d8c", "Quantity × unit price"),
    ]:
        values = [float(v) for v in lines[column]]
        bars = ax.barh([i + offset for i in y], values, height=0.3, color=color, label=label)
        ax.bar_label(bars, labels=[f"{v:.2f}" for v in values], padding=4, fontsize=10)
    ax.set_yticks(y, lines.sku)
    ax.set_ylim(len(lines) - 0.5, -0.5)
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, max(float(lines.printed_amount.max()), float(lines.computed_amount.max())) * 1.23)
    ax.set_xlabel("USD · invented values, no tax or other adjustments")
    fig.legend(
        *ax.get_legend_handles_labels(),
        loc="upper center",
        bbox_to_anchor=(0.55, 0.82),
        ncol=2,
        frameon=False,
        fontsize=10,
    )
    finish(
        fig,
        "A clean extraction does not guarantee correct arithmetic",
        f"Synthetic page {PAGE_NUMBER} · discrepant lines: {int(lines.discrepancy.sum())} · exact SQL decimals",
        "Source: authored two-page invoice fixture · invented USD values, not a real transaction\n"
        "VGI PDF v0.4.0 extraction/rendering + visible SQL; no OCR or general invoice parsing",
        name="02-invoice-amounts",
    )


def intake_accounting(inventory, routes, required_field):
    """Keep unavailable map results distinct from valid zero counts."""
    import pandas as pd
    from matplotlib.colors import ListedColormap
    from matplotlib.patches import Patch

    columns = ["page_count", "encrypted", "metadata_entries", "form_field_count"]
    codes = []
    labels = []
    for row in inventory.itertuples():
        values = [getattr(row, c) for c in columns]
        code = []
        text = []
        for name, value in zip(columns, values, strict=True):
            if pd.isna(value):
                code.append(0)
                text.append("NULL")
            elif name == "encrypted":
                code.append(2 if value else 1)
                text.append("Yes" if value else "No")
            else:
                code.append(1)
                text.append(str(int(value)))
        codes.append(code)
        labels.append(text)
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 7.1), gridspec_kw={"width_ratios": [1.1, 1]})
    matrix, bars_ax = axes
    matrix.imshow(codes, cmap=ListedColormap(["#e4e7e5", "#cee2d8", "#e8c4a8"]), vmin=0, vmax=2, aspect="auto")
    for y, row in enumerate(labels):
        for x, label in enumerate(row):
            matrix.text(x, y, label, ha="center", va="center", fontsize=11, color="#294439")
    matrix.set_xticks(range(4), ["Pages", "Encrypted?", "Metadata\nentries", "Form\nfields"])
    matrix.set_yticks(range(len(inventory)), inventory.document_label)
    matrix.set_title("Actual scalar results for six inputs")
    matrix.grid(False)
    bars = bars_ax.bar(range(5), routes.documents, color=["#397d62", "#b47b46", "#d8b18d", "#879d95", "#bac5be"])
    bars_ax.bar_label(bars, padding=4)
    bars_ax.set_xticks(
        range(5),
        ["Ready for\nreview", "Field absent\nor blank", "Encrypted", "Unreadable\nbytes", "Missing\ninput"],
        fontsize=9,
    )
    bars_ax.set(ylabel="Documents (count)", ylim=(0, 6), title=f"Review rule: {required_field}")
    bars_ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    fig.legend(
        handles=[
            Patch(facecolor="#cee2d8", label="Available result, including zero"),
            Patch(facecolor="#e4e7e5", label="Unavailable (NULL)"),
            Patch(facecolor="#e8c4a8", label="Encryption detected"),
        ],
        loc="upper center",
        bbox_to_anchor=(0.53, 0.82),
        ncol=3,
        frameon=False,
        fontsize=10,
    )
    finish(
        fig,
        "An empty form is different from unavailable extraction",
        f"Six synthetic inputs · selected field: {required_field} · all five routing categories retained",
        "Source: released PDF test fixtures, authored invoice and encrypted derivative · no real customer documents\n"
        "VGI PDF v0.4.0 · metadata claims are not verified authorship; ready means human review, not acceptance",
        name="03-document-intake",
    )
