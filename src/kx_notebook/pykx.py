"""Compatibility helpers for the opt-in PyKX adapter."""

from __future__ import annotations

from typing import Any, Callable, Optional

from .contract import DEFAULT_BYTE_LIMIT, DEFAULT_ROW_LIMIT
from .evaluators import PyKXEvaluator
from .magic import configure_evaluator


def configure_pykx(
    q: Optional[Callable[[str], Any]] = None,
    *,
    label: str = "PyKX q in this Python kernel",
    row_limit: int = DEFAULT_ROW_LIMIT,
    byte_limit: int = DEFAULT_BYTE_LIMIT,
    include_q_source: bool = False,
) -> None:
    """Select PyKX explicitly; importing :mod:`kx_notebook` never imports PyKX."""

    configure_evaluator(
        PyKXEvaluator(q),
        label=label,
        row_limit=row_limit,
        byte_limit=byte_limit,
        include_q_source=include_q_source,
    )
