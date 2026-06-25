"""Shared per-object discovery/description metadata for the ``pdf`` worker.

The ``vgi-lint`` strict profile expects these tags on **every** function and
table object, surfaced through each function's ``Meta.tags``:

- ``vgi.title`` (VGI124)      -- human-friendly display name (must NOT
  normalize-equal the machine name, so it carries an extra descriptive word).
- ``vgi.doc_llm`` (VGI112)    -- a Markdown narrative aimed at an LLM/agent:
  what the object does, when to use it, its inputs/outputs and edge cases.
- ``vgi.doc_md`` (VGI113)     -- a Markdown narrative for human docs: an
  overview plus usage and notes. DISTINCT content from ``doc_llm``.
- ``vgi.keywords`` (VGI126/VGI138) -- a JSON array of search-term strings.

Per VGI139 the ``vgi.source_url`` tag is intentionally NOT set on individual
objects: a source link belongs only on the catalog object (set via the
``Catalog(source_url=...)`` argument), and a per-object copy is redundant.

:func:`keywords_json` serializes a list of keyword strings into the JSON-array
form VGI138 requires; :func:`object_tags` assembles the standard per-object
tags into a ``dict[str, str]``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence


def keywords_json(keywords: Sequence[str]) -> str:
    """Serialize keyword strings into the ``vgi.keywords`` JSON-array form.

    VGI138 requires ``vgi.keywords`` to be a JSON array of strings (e.g.
    ``["pdf","tables"]``), not a comma-separated string. This renders the list
    as compact JSON so the tag value parses back to the same list.
    """
    return json.dumps(list(keywords), separators=(",", ":"))


def object_tags(
    *,
    title: str,
    doc_llm: str,
    doc_md: str,
    keywords: Sequence[str],
    relative_path: str,
) -> dict[str, str]:
    """Assemble the standard per-object discovery/description tags.

    Args:
        title: Human-friendly display name (``vgi.title``).
        doc_llm: LLM/agent-oriented Markdown narrative (``vgi.doc_llm``).
        doc_md: Human-doc Markdown narrative (``vgi.doc_md``).
        keywords: Search terms / synonyms, serialized as a JSON array
            (``vgi.keywords``).
        relative_path: Implementing source file, relative to the repo root.
            Retained for documentation/back-compat; no per-object
            ``vgi.source_url`` tag is emitted (VGI139 -- the source link lives
            only on the catalog).

    Returns:
        A ``dict`` of the standard tag keys to their string values.
    """
    del relative_path  # no per-object source_url (VGI139)
    return {
        "vgi.title": title,
        "vgi.doc_llm": doc_llm,
        "vgi.doc_md": doc_md,
        "vgi.keywords": keywords_json(keywords),
    }
