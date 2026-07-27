"""Versioned, bounded, portable KX notebook result contract.

The v1 shape intentionally matches ``vscode-kdb/src/notebook-contract.ts``.
Only bounded previews are persisted in notebook output.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import decimal
import itertools
import json
import math
from collections.abc import Iterable, Mapping, Sequence, Sized
from dataclasses import dataclass
from typing import Any, Optional

MIME_TYPE = "application/vnd.kx.result+json"
CONTRACT_VERSION = 1
DEFAULT_ROW_LIMIT = 20
DEFAULT_BYTE_LIMIT = 1_000_000
MIN_BYTE_LIMIT = 16_384
MAX_BYTE_LIMIT = 10_000_000
MAX_ROW_LIMIT = 10_000
MAX_COLUMNS = 256
MAX_STRING_CHARS = 32_768
MAX_QTEXT_CHARS = 1_048_576
MAX_LABEL_CHARS = 200
MAX_Q_SOURCE_CHARS = 4_000
MAX_JSON_DEPTH = 12
MAX_JSON_ITEMS = 2_000
MAX_INTEGER_BITS = 13_000
JS_SAFE_INTEGER = (1 << 53) - 1


class KxNotebookError(ValueError):
    """Base class for invalid portable notebook output."""


class TableShapeError(KxNotebookError):
    """A value cannot be represented as a supported table."""


class OutputLimitError(KxNotebookError):
    """Even a zero-row or empty-text result cannot fit its byte limit."""


@dataclass(frozen=True)
class Chart:
    """Static chart selection understood by the existing vscode-kdb renderer."""

    type: str
    x_column: str
    y_columns: tuple[str, ...]
    title: Optional[str] = None
    group_by_column: Optional[str] = None
    open_column: Optional[str] = None
    high_column: Optional[str] = None
    low_column: Optional[str] = None
    close_column: Optional[str] = None


@dataclass(frozen=True)
class QText:
    """Bounded textual representation for non-tabular or unsupported q values."""

    text: str
    truncated: bool = False
    truncation_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvaluationResult:
    """Evaluator output plus optional table/display metadata."""

    value: Any
    columns: Optional[Sequence[str]] = None
    row_count: Optional[int] = None
    label: Optional[str] = None
    chart: Optional[Chart] = None


@dataclass(frozen=True)
class PortableOutput:
    """A rich MIME bundle and the measured size of all three MIME bodies."""

    bundle: dict[str, Any]
    body_bytes: int


@dataclass
class _StringState:
    truncated: bool = False


@dataclass
class _Rows:
    columns: list[str]
    row_count: int
    available_count: int
    iterator: Iterable[Sequence[Any]]


def canonical_payload_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return deterministic UTF-8 JSON used for byte accounting."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def build_mime_bundle(
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
) -> PortableOutput:
    """Create a strict v1 KX MIME bundle with HTML and plain-text fallbacks."""

    row_limit = _bounded_int("row_limit", row_limit, 1, MAX_ROW_LIMIT)
    byte_limit = _bounded_int("byte_limit", byte_limit, MIN_BYTE_LIMIT, MAX_BYTE_LIMIT)
    label = _optional_text("label", label, MAX_LABEL_CHARS)
    q_source = _optional_text("q_source", q_source, MAX_Q_SOURCE_CHARS)
    elapsed_ms = _elapsed(elapsed_ms)
    if marker not in {"%%q", "direct-ipc"}:
        raise KxNotebookError("marker must be '%%q' or 'direct-ipc'")
    if isinstance(value, QText):
        if columns is not None or row_count is not None or chart is not None:
            raise KxNotebookError("qText results cannot have table metadata or charts")
        return _qtext_bundle(
            value,
            byte_limit=byte_limit,
            label=label,
            elapsed_ms=elapsed_ms,
            q_source=q_source,
            marker=marker,
        )

    rows = _table_rows(value, columns=columns, row_count=row_count)
    normalized_chart = _chart(chart, rows.columns)
    count = min(rows.row_count, rows.available_count, row_limit)
    typed: list[list[dict[str, Any]]] = []
    truncated_at: list[bool] = []
    column_kinds: list[set[str]] = [set() for _ in rows.columns]
    strings = _StringState()
    iterator = iter(rows.iterator)
    normalized_bytes = 0
    normalization_byte_truncated = False
    for row_index in range(count):
        try:
            raw_row = next(iterator)
        except StopIteration as error:
            raise TableShapeError(
                f"row_count={rows.row_count} but input ended at row {row_index}"
            ) from error
        if isinstance(raw_row, (str, bytes, bytearray)) or not isinstance(raw_row, Sequence):
            raise TableShapeError(f"row {row_index} must be a sequence")
        if len(raw_row) != len(rows.columns):
            raise TableShapeError(
                f"row {row_index} has {len(raw_row)} cells; expected {len(rows.columns)}"
            )
        row = [raw_row[index] for index in range(len(rows.columns))]
        typed_row = [
            _typed_cell(
                cell,
                strings,
                path=f"row {row_index}, column {column_index}",
            )
            for column_index, cell in enumerate(row)
        ]
        for column_index, cell in enumerate(typed_row):
            if cell["kind"] != "null":
                column_kinds[column_index].add(str(cell["kind"]))
        row_bytes = len(
            json.dumps(
                typed_row,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        if normalized_bytes + row_bytes > byte_limit:
            normalization_byte_truncated = True
            break
        normalized_bytes += row_bytes
        typed.append(typed_row)
        truncated_at.append(strings.truncated)

    base_reasons: list[str] = []
    if rows.row_count > row_limit:
        base_reasons.append("rowLimit")
    if rows.available_count < min(rows.row_count, row_limit):
        base_reasons.append("sourcePreview")
    if normalization_byte_truncated:
        base_reasons.append("byteLimit")
    schema = [
        {"name": name, "type": _column_type(column_kinds[index])}
        for index, name in enumerate(rows.columns)
    ]

    def candidate(preview_count: int, byte_truncated: bool) -> PortableOutput:
        reasons = list(base_reasons)
        if preview_count and truncated_at[preview_count - 1]:
            reasons.append("cellValueLimit")
        if byte_truncated:
            reasons.append("byteLimit")
        payload: dict[str, Any] = {
            "version": CONTRACT_VERSION,
            "kind": "table",
            "schema": {"columns": schema},
            "data": {"encoding": "rows", "rows": typed[:preview_count]},
            "result": {
                "rowCount": rows.row_count,
                "previewRowCount": preview_count,
                "truncated": bool(reasons) or preview_count < rows.row_count,
                "truncationReasons": list(dict.fromkeys(reasons)),
                "rowLimit": row_limit,
                "byteLimit": byte_limit,
            },
            "provenance": _provenance(marker, label, elapsed_ms, q_source),
        }
        if normalized_chart is not None:
            payload["chart"] = normalized_chart
        return _assemble(payload)

    materialized_count = len(typed)
    full = candidate(materialized_count, False)
    if full.body_bytes <= byte_limit:
        return full
    low, high = 0, materialized_count - 1
    accepted: Optional[PortableOutput] = None
    while low <= high:
        middle = (low + high) // 2
        current = candidate(middle, True)
        if current.body_bytes <= byte_limit:
            accepted = current
            low = middle + 1
        else:
            high = middle - 1
    if accepted is None:
        raise OutputLimitError("byte_limit is too small for the schema and zero-row fallbacks")
    return accepted


def _qtext_bundle(
    value: QText,
    *,
    byte_limit: int,
    label: Optional[str],
    elapsed_ms: Optional[float],
    q_source: Optional[str],
    marker: str,
) -> PortableOutput:
    original = value.text
    if not isinstance(original, str):
        raise KxNotebookError("QText.text must be a string")
    _validate_text("QText.text", original)
    clipped_for_limit = len(original) > MAX_QTEXT_CHARS
    source = _clip(original, MAX_QTEXT_CHARS)
    inherited = [
        reason
        for reason in value.truncation_reasons
        if reason in {"rowLimit", "byteLimit", "cellValueLimit", "columnLimit", "sourcePreview"}
    ]
    base_reasons = list(dict.fromkeys(inherited))
    if clipped_for_limit and "cellValueLimit" not in base_reasons:
        base_reasons.append("cellValueLimit")
    elif value.truncated and not base_reasons:
        base_reasons.append("cellValueLimit")

    def candidate(char_count: int, byte_truncated: bool) -> PortableOutput:
        text = source[:char_count]
        if char_count < len(source):
            text = text[:-1] + "…" if text else ""
        reasons = list(base_reasons)
        if byte_truncated and "byteLimit" not in reasons:
            reasons.append("byteLimit")
        payload = {
            "version": CONTRACT_VERSION,
            "kind": "qText",
            "data": {"text": text},
            "result": {
                "truncated": bool(reasons),
                "truncationReasons": reasons,
                "byteLimit": byte_limit,
            },
            "provenance": _provenance(marker, label, elapsed_ms, q_source),
        }
        return _assemble(payload)

    full = candidate(len(source), False)
    if full.body_bytes <= byte_limit:
        return full
    low, high = 0, len(source)
    accepted: Optional[PortableOutput] = None
    while low <= high:
        middle = (low + high) // 2
        current = candidate(middle, True)
        if current.body_bytes <= byte_limit:
            accepted = current
            low = middle + 1
        else:
            high = middle - 1
    if accepted is None:
        raise OutputLimitError("byte_limit is too small for qText fallbacks")
    return accepted


def _assemble(payload: dict[str, Any]) -> PortableOutput:
    from .fallback import static_html, static_text

    html = static_html(payload)
    text = static_text(payload)
    bundle = {MIME_TYPE: payload, "text/html": html, "text/plain": text}
    size = (
        len(canonical_payload_bytes(payload))
        + len(html.encode("utf-8"))
        + len(text.encode("utf-8"))
    )
    return PortableOutput(bundle, size)


def _provenance(
    marker: str,
    label: Optional[str],
    elapsed_ms: Optional[float],
    q_source: Optional[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {"marker": marker}
    if label is not None:
        result["label"] = label
    if elapsed_ms is not None:
        result["elapsedMs"] = elapsed_ms
    if q_source is not None:
        result["qSource"] = q_source
    return result


def _table_rows(
    value: Any,
    *,
    columns: Optional[Sequence[str]],
    row_count: Optional[int],
) -> _Rows:
    names = _column_names(columns) if columns is not None else None
    total = _row_count(row_count)
    if isinstance(value, Mapping):
        available_names = _mapping_columns(value)
        selected = names or available_names
        if set(selected) != set(available_names):
            raise TableShapeError("columns must exactly match the column mapping keys")
        vectors: list[Sequence[Any]] = []
        lengths: list[int] = []
        for name in selected:
            if name not in value:
                raise TableShapeError(f"column {name!r} is missing")
            vector = value[name]
            if (
                isinstance(vector, (str, bytes, bytearray))
                or not hasattr(vector, "__len__")
                or not hasattr(vector, "__getitem__")
            ):
                raise TableShapeError(f"column {name!r} must be a sequence")
            vectors.append(vector)
            lengths.append(len(vector))
        inferred = lengths[0] if lengths else 0
        if any(length != inferred for length in lengths):
            raise TableShapeError("column mapping contains unequal lengths")
        bounded_inferred = _row_count(inferred)
        assert bounded_inferred is not None
        actual_total = bounded_inferred if total is None else total
        if actual_total < inferred:
            raise TableShapeError("row_count cannot be less than supplied rows")
        return _Rows(
            selected,
            actual_total,
            bounded_inferred,
            (tuple(vector[index] for vector in vectors) for index in range(bounded_inferred)),
        )

    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Iterable):
        raise TableShapeError("table value must be an iterable of rows")
    available = _row_count(len(value)) if isinstance(value, Sized) else total
    iterator = iter(value)
    try:
        first = next(iterator)
    except StopIteration:
        selected = names or []
        actual_total = total if total is not None else 0
        if actual_total:
            raise TableShapeError("non-zero row_count was supplied for an empty table") from None
        return _Rows(selected, 0, 0, ())

    def with_first() -> Iterable[Any]:
        yield first
        yield from iterator

    resolved_names = names
    if isinstance(first, Mapping):
        first_names = _mapping_columns(first)
        resolved_names = names or first_names
        if set(resolved_names) != set(first_names):
            raise TableShapeError("columns must exactly match the row mapping keys")
        expected_names = set(resolved_names)

        def mapping_rows() -> Iterable[Sequence[Any]]:
            for index, item in enumerate(with_first()):
                if not isinstance(item, Mapping):
                    raise TableShapeError(f"row {index} is not a mapping")
                actual_names = _mapping_columns(item)
                if set(actual_names) != expected_names:
                    raise TableShapeError(
                        f"row {index} keys do not exactly match the table columns"
                    )
                yield tuple(item[name] for name in resolved_names or ())

        rows: Iterable[Sequence[Any]] = mapping_rows()
    else:
        if names is None:
            raise TableShapeError("columns are required for sequence rows")
        if isinstance(first, (str, bytes, bytearray)) or not isinstance(first, Sequence):
            raise TableShapeError("each table row must be a sequence or mapping")
        rows = with_first()
    if available is None:
        raise TableShapeError("row_count is required for an unsized iterable")
    actual_total = int(available) if total is None else total
    if actual_total < int(available):
        raise TableShapeError("row_count cannot be less than supplied rows")
    return _Rows(resolved_names or [], actual_total, int(available), rows)


def _typed_cell(value: Any, state: _StringState, path: str) -> dict[str, Any]:
    if value is None:
        return {"kind": "null"}
    if isinstance(value, bool):
        return {"kind": "boolean", "value": value}
    if isinstance(value, int):
        if abs(value) > JS_SAFE_INTEGER:
            if value.bit_length() > MAX_INTEGER_BITS:
                raise KxNotebookError(f"{path} integer is too large to serialize safely")
            return {"kind": "bigint", "value": _clip_value(str(value), state)}
        return {"kind": "number", "value": value}
    if isinstance(value, decimal.Decimal):
        return {"kind": "string", "value": _clip_value(str(value), state)}
    if isinstance(value, float):
        number = float(value)
        if math.isfinite(number):
            return {"kind": "number", "value": number}
        return {"kind": "string", "value": _clip_value(str(value), state)}
    if isinstance(value, (dt.datetime, dt.date, dt.time, dt.timedelta)):
        return {"kind": "temporal", "value": _clip_value(_temporal(value), state)}
    # Direct IPC temporals deliberately expose a small, dependency-free contract.
    if getattr(value, "__kx_temporal__", False):
        return {"kind": "temporal", "value": _clip_value(str(value), state)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"kind": "string", "value": _binary_text(value, state)}
    if isinstance(value, str):
        return {"kind": "string", "value": _clip_value(value, state)}
    try:
        normalized = _json_value(value, 0, [0], state, [0])
        serialized = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise KxNotebookError(f"{path} is not safely serializable") from None
    return {"kind": "json", "value": _clip_value(serialized, state)}


def _json_value(
    value: Any,
    depth: int,
    items: list[int],
    state: _StringState,
    chars: list[int],
) -> Any:
    if depth > MAX_JSON_DEPTH:
        raise KxNotebookError("nested cell exceeds maximum JSON depth")
    items[0] += 1
    if items[0] > MAX_JSON_ITEMS:
        raise KxNotebookError("nested cell exceeds maximum JSON item count")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _json_text(value, state, chars)
    if isinstance(value, int):
        if value.bit_length() > MAX_INTEGER_BITS:
            raise KxNotebookError("nested integer is too large to serialize safely")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(
                getattr(value, field.name),
                depth + 1,
                items,
                state,
                chars,
            )
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        mapped: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = _json_text(str(key), state, chars)
            if normalized_key in mapped:
                raise KxNotebookError("nested mapping keys collide after string conversion")
            mapped[normalized_key] = _json_value(
                item,
                depth + 1,
                items,
                state,
                chars,
            )
        return mapped
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item, depth + 1, items, state, chars) for item in value]
    raise TypeError(f"unsupported {type(value).__name__}")


def _json_text(
    value: str,
    state: _StringState,
    chars: list[int],
) -> str:
    _validate_text("nested string", value)
    remaining = max(0, MAX_STRING_CHARS - chars[0])
    if len(value) > remaining:
        state.truncated = True
        value = _clip(value, remaining) if remaining else ""
    chars[0] += len(value)
    return value


def _column_type(kinds: set[str]) -> str:
    return next(iter(kinds)) if len(kinds) == 1 else ("null" if not kinds else "mixed")


def _column_names(columns: Sequence[Any]) -> list[str]:
    if isinstance(columns, (str, bytes, bytearray)):
        raise TableShapeError("columns must be a sequence of names")
    if len(columns) > MAX_COLUMNS:
        raise TableShapeError(f"tables support at most {MAX_COLUMNS} columns")
    result: list[str] = []
    used: set[str] = set()
    for index, raw in enumerate(columns):
        if not isinstance(raw, str) or not raw:
            raise TableShapeError(f"column {index} must be a non-empty string")
        _validate_text(f"column {index}", raw)
        if len(raw) > 256:
            raise TableShapeError(f"column {index} must contain at most 256 characters")
        name = raw
        if name in used:
            raise TableShapeError(f"duplicate column name {name!r}")
        used.add(name)
        result.append(name)
    return result


def _mapping_columns(value: Mapping[Any, Any]) -> list[str]:
    keys = list(itertools.islice(value, MAX_COLUMNS + 1))
    return _column_names(keys)


def _binary_text(
    value: bytes | bytearray | memoryview,
    state: _StringState,
) -> str:
    raw_length = value.nbytes if isinstance(value, memoryview) else len(value)
    input_limit = (MAX_STRING_CHARS // 4) * 3
    try:
        if isinstance(value, memoryview):
            prefix = value.cast("B")[:input_limit].tobytes()
        else:
            prefix = bytes(value[:input_limit])
    except (TypeError, ValueError):
        raise KxNotebookError("binary cell must be a contiguous byte sequence") from None
    encoded = base64.b64encode(prefix).decode("ascii")
    if raw_length > input_limit:
        state.truncated = True
        return encoded[: MAX_STRING_CHARS - 1] + "…"
    return encoded


def _chart(chart: Optional[Chart], columns: list[str]) -> Optional[dict[str, Any]]:
    if chart is None:
        return None
    if chart.type not in {"line", "scatter", "step", "bar", "box", "candlestick"}:
        raise KxNotebookError(f"unsupported chart type {chart.type!r}")
    if len(chart.y_columns) > MAX_COLUMNS or len(set(chart.y_columns)) != len(chart.y_columns):
        raise KxNotebookError("chart y columns must be unique and bounded")
    needed = [chart.x_column, *chart.y_columns]
    optional = [
        chart.group_by_column,
        chart.open_column,
        chart.high_column,
        chart.low_column,
        chart.close_column,
    ]
    for name in [*needed, *(item for item in optional if item)]:
        if name not in columns:
            raise KxNotebookError(f"chart column {name!r} is unavailable")
    if chart.type == "candlestick":
        if not all((chart.open_column, chart.high_column, chart.low_column, chart.close_column)):
            raise KxNotebookError("candlestick requires open/high/low/close columns")
    elif not chart.y_columns:
        raise KxNotebookError(f"{chart.type} requires at least one y column")
    result: dict[str, Any] = {
        "version": 1,
        "visible": True,
        "type": chart.type,
        "xColumn": chart.x_column,
        "yColumns": list(chart.y_columns),
    }
    for field, key in (
        ("group_by_column", "groupByColumn"),
        ("open_column", "openColumn"),
        ("high_column", "highColumn"),
        ("low_column", "lowColumn"),
        ("close_column", "closeColumn"),
    ):
        item = getattr(chart, field)
        if item is not None:
            result[key] = item
    if chart.title is not None:
        result["title"] = _optional_text("chart title", chart.title, MAX_LABEL_CHARS)
    return result


def _bounded_int(name: str, value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise KxNotebookError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise KxNotebookError(f"{name} must be between {minimum} and {maximum}")
    return int(value)


def _row_count(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    return _bounded_int("row_count", value, 0, (1 << 53) - 1)


def _optional_text(name: str, value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise KxNotebookError(f"{name} must be a string")
    _validate_text(name, value)
    if len(value) > limit:
        raise KxNotebookError(f"{name} must contain at most {limit} characters")
    return value


def _elapsed(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KxNotebookError("elapsed_ms must be numeric")
    number = float(value)
    if number < 0 or not math.isfinite(number):
        raise KxNotebookError("elapsed_ms must be finite and non-negative")
    return round(number, 3)


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _clip_value(value: str, state: _StringState) -> str:
    _validate_text("cell text", value)
    if len(value) > MAX_STRING_CHARS:
        state.truncated = True
        return _clip(value, MAX_STRING_CHARS)
    return value


def _validate_text(name: str, value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise KxNotebookError(f"{name} must contain valid UTF-8 text") from None


def _temporal(value: Any) -> str:
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            return value.isoformat().replace("+00:00", "Z")
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    return str(value.isoformat())
