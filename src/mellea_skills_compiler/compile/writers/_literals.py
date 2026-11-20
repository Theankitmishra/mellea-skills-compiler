"""Shared Python literal rendering helpers for deterministic writers."""

from __future__ import annotations

import ast
from typing import Any


def _safe_triple_quoted(value: str) -> str | None:
    """Return a triple-quoted literal for a multi-line string if it round-trips cleanly."""
    if "\n" not in value or "\\" in value or '"""' in value or value.endswith('"'):
        return None
    candidate = f'"""{value}"""'
    try:
        evaluated = ast.literal_eval(candidate)
    except (SyntaxError, ValueError):
        return None
    return candidate if evaluated == value else None


def render_literal(value: Any) -> str:
    """Render any JSON-derived value as a valid Python literal.

    Multi-line strings use triple quotes only when they contain no backslashes,
    do not end with a double quote, and round-trip identically under
    ``ast.literal_eval``; all other strings fall back to ``repr()``. Dicts and
    lists recurse so nested multi-line strings stay readable.
    """
    if isinstance(value, str):
        triple = _safe_triple_quoted(value)
        return triple if triple is not None else repr(value)
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [f"{render_literal(k)}: {render_literal(v)}" for k, v in value.items()]
        return "{" + ", ".join(items) + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        return "[" + ", ".join(render_literal(v) for v in value) + "]"
    return repr(value)
