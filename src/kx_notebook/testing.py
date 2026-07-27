"""Small deterministic callback fixture for examples and downstream tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class FixtureEvaluator:
    """Map exact q source strings to fixed values; this is not a q parser."""

    def __init__(self, fixtures: Mapping[str, Any]) -> None:
        self._fixtures = dict(fixtures)

    def __call__(self, source: str) -> Any:
        try:
            return self._fixtures[source]
        except KeyError:
            raise KeyError(f"No fixture is registered for exact q source {source!r}") from None
