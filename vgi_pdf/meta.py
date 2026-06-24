"""Shared per-object discovery/description metadata for the ``pdf`` worker.

The ``vgi-lint`` strict profile (0.26.0) expects these tags on **every**
function and table object, surfaced through each function's ``Meta.tags``:

- ``vgi.title`` (VGI124)      -- human-friendly display name (must NOT
  normalize-equal the machine name, so it carries an extra descriptive word).
- ``vgi.doc_llm`` (VGI112)    -- a Markdown narrative aimed at an LLM/agent:
  what the object does, when to use it, its inputs/outputs and edge cases.
- ``vgi.doc_md`` (VGI113)     -- a Markdown narrative for human docs: an
  overview plus usage and notes. DISTINCT content from ``doc_llm``.
- ``vgi.keywords`` (VGI126)   -- comma-separated search terms / synonyms.
- ``vgi.source_url`` (VGI128) -- link to the implementing source file.

:func:`source_url` builds the canonical GitHub blob URL for a source file so
every object points at exactly where it is implemented; :func:`object_tags`
assembles the five standard tags into a ``dict[str, str]``.
"""

from __future__ import annotations

# Base GitHub blob URL for source files in this repo (pinned to ``main``).
SOURCE_BASE = "https://github.com/Query-farm/vgi-pdf/blob/main"


def source_url(relative_path: str) -> str:
    """Build the ``vgi.source_url`` for a file at ``relative_path`` in the repo.

    For example ``source_url("vgi_pdf/scalars.py")`` ->
    ``https://github.com/Query-farm/vgi-pdf/blob/main/vgi_pdf/scalars.py``.
    """
    return f"{SOURCE_BASE}/{relative_path}"


def object_tags(
    *,
    title: str,
    doc_llm: str,
    doc_md: str,
    keywords: str,
    relative_path: str,
) -> dict[str, str]:
    """Assemble the five standard per-object discovery/description tags.

    Args:
        title: Human-friendly display name (``vgi.title``).
        doc_llm: LLM/agent-oriented Markdown narrative (``vgi.doc_llm``).
        doc_md: Human-doc Markdown narrative (``vgi.doc_md``).
        keywords: Comma-separated search terms / synonyms (``vgi.keywords``).
        relative_path: Implementing source file, relative to the repo root.

    Returns:
        A ``dict`` of the five tag keys to their string values.
    """
    return {
        "vgi.title": title,
        "vgi.doc_llm": doc_llm,
        "vgi.doc_md": doc_md,
        "vgi.keywords": keywords,
        "vgi.source_url": source_url(relative_path),
    }
