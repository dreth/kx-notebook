"""IPython rich-display entry point."""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from .contract import (
    DEFAULT_BYTE_LIMIT,
    DEFAULT_ROW_LIMIT,
    MIME_TYPE,
    Chart,
    PortableOutput,
    QText,
    build_mime_bundle,
    canonical_payload_bytes,
)


def display_result(
    value: Any,
    *,
    columns: Optional[Sequence[str]] = None,
    row_count: Optional[int] = None,
    label: Optional[str] = None,
    elapsed_ms: Optional[float] = None,
    q_source: Optional[str] = None,
    row_limit: int = DEFAULT_ROW_LIMIT,
    byte_limit: int = DEFAULT_BYTE_LIMIT,
    chart: Optional[Chart] = None,
    marker: str = "%%q",
    redact_text: Optional[Callable[[str], str]] = None,
) -> PortableOutput:
    """Publish one raw MIME bundle.

    Embedders that bypass ``%%q`` should pass their evaluator's ``redact_text``
    method so the final serialized representations receive the credential gate.
    """

    from IPython.display import display

    output = build_mime_bundle(
        value,
        columns=columns,
        row_count=row_count,
        label=label,
        elapsed_ms=elapsed_ms,
        q_source=q_source,
        row_limit=row_limit,
        byte_limit=byte_limit,
        chart=chart,
        marker=marker,
    )
    serialized: Sequence[str] = (
        canonical_payload_bytes(output.bundle[MIME_TYPE]).decode("utf-8"),
        output.bundle["text/html"],
        output.bundle["text/plain"],
    )
    if redact_text is not None and _redactor_matches(redact_text, serialized):
        # Remove the original before constructing a fixed, content-free notice.
        output.bundle.clear()
        serialized = ()
        safe_byte_limit = byte_limit
        value = None
        columns = None
        row_count = None
        label = None
        elapsed_ms = None
        q_source = None
        row_limit = 0
        byte_limit = 0
        chart = None
        marker = ""
        output = build_mime_bundle(
            QText(
                "[KX result omitted: serialized output matched a runtime credential]",
                truncated=True,
                truncation_reasons=("sourcePreview",),
            ),
            byte_limit=safe_byte_limit,
        )
        serialized = (
            canonical_payload_bytes(output.bundle[MIME_TYPE]).decode("utf-8"),
            output.bundle["text/html"],
            output.bundle["text/plain"],
        )
        if _redactor_matches(redact_text, serialized):
            # A credential can coincide with fixed contract/template text. In
            # that degenerate case, suppress output rather than leak or raise
            # through frames that still belong to the caller.
            output.bundle.clear()
            output = PortableOutput({}, 0)
            serialized = ()
            redact_text = None
            return output
        serialized = ()
        redact_text = None
    display(output.bundle, raw=True)
    return output


def _redactor_matches(redactor: Callable[[str], str], values: Sequence[str]) -> bool:
    try:
        return any(redactor(value) != value for value in values)
    except Exception:
        # A configured credential gate that cannot inspect output must fail closed.
        return True
