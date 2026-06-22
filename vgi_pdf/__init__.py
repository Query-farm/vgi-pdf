"""Extract PDF *structure* — tables, word boxes, geometry, forms, rendering — as a VGI worker.

The implementation is split so each concern stays focused:

- ``core``    -- pure PDF parsing/rendering logic over ``pdfplumber`` (MIT),
  ``pypdfium2`` (Apache-2.0) and ``pikepdf`` (MPL-2.0); no Arrow or VGI
  dependency, directly unit-testable. Hostile-input safe: every parse/render is
  wrapped so a malformed/encrypted/bomb PDF can never crash the worker.
- ``scalars`` -- per-row VGI scalar functions (positional-only; the polymorphic
  ``pdf`` input -- a VARCHAR path or a BLOB of bytes -- and the optional ``dpi``
  argument are exposed as arity / input-type overloads).
- ``tables``  -- set-returning table functions: ``tables``, ``words``, ``pages``.

``pdf_worker.py`` at the repo root assembles these into the ``pdf`` catalog and
runs the worker over stdio (or HTTP). This is deliberately *not* ``vgi-tika``:
tika does plain text; ``vgi-pdf`` does layout / tables / coordinates / rendering.
"""

from __future__ import annotations

__version__ = "0.1.0"
