"""Evaluator abstractions for direct IPC, callbacks, PyKX, and a local broker."""

from __future__ import annotations

import http.client
import inspect
import ipaddress
import json
import math
import socket
import threading
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, Union, runtime_checkable

from .contract import (
    DEFAULT_BYTE_LIMIT,
    DEFAULT_ROW_LIMIT,
    JS_SAFE_INTEGER,
    MAX_BYTE_LIMIT,
    MAX_COLUMNS,
    MAX_LABEL_CHARS,
    MAX_ROW_LIMIT,
    MIN_BYTE_LIMIT,
    EvaluationResult,
    QText,
)
from .ipc import (
    DEFAULT_MAX_RECEIVE_BYTES,
    DIRECT_Q_ENVELOPE_MARKER,
    DIRECT_Q_MAX_PREVIEW_CELLS,
    QCharVector,
    QConnection,
    QDictionary,
    QKeyedTable,
    QSymbol,
    QTable,
    QVector,
    q_script_query,
    q_text,
    redact_q_value,
)


class EvaluatorError(RuntimeError):
    """Evaluator configuration or adapter contract failure."""


@dataclass(frozen=True)
class EvaluationContext:
    """Per-cell limits and optional query timeout."""

    row_limit: int = DEFAULT_ROW_LIMIT
    byte_limit: int = DEFAULT_BYTE_LIMIT
    timeout: Optional[float] = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.row_limit, bool)
            or not isinstance(self.row_limit, int)
            or not 1 <= self.row_limit <= MAX_ROW_LIMIT
        ):
            raise ValueError(f"row_limit must be between 1 and {MAX_ROW_LIMIT}")
        if (
            isinstance(self.byte_limit, bool)
            or not isinstance(self.byte_limit, int)
            or not MIN_BYTE_LIMIT <= self.byte_limit <= MAX_BYTE_LIMIT
        ):
            raise ValueError(f"byte_limit must be between {MIN_BYTE_LIMIT} and {MAX_BYTE_LIMIT}")
        if self.timeout is not None:
            _positive_timeout(self.timeout)


@runtime_checkable
class Evaluator(Protocol):
    """Typed synchronous evaluator contract."""

    def evaluate(
        self, source: str, context: Optional[EvaluationContext] = None
    ) -> EvaluationResult:
        """Evaluate exact q source and return portable display input."""

    def close(self) -> None:
        """Release evaluator resources."""


EvaluatorLike = Union[Evaluator, Callable[[str], Any]]


class CallbackEvaluator:
    """Adapt an explicitly supplied synchronous Python callback."""

    def __init__(self, callback: Callable[[str], Any]) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._callback = callback

    def evaluate(
        self, source: str, context: Optional[EvaluationContext] = None
    ) -> EvaluationResult:
        del context
        result = self._callback(source)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise EvaluatorError(
                "the callback returned an awaitable; synchronous evaluators are required"
            )
        return result if isinstance(result, EvaluationResult) else EvaluationResult(result)

    def close(self) -> None:
        close = getattr(self._callback, "close", None)
        if callable(close):
            close()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(callback=<configured>)"


class DirectQEvaluator:
    """Persistent dependency-free q IPC evaluator."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        username: str = "",
        password: Optional[str] = None,
        connect_timeout: Optional[float] = 5.0,
        query_timeout: Optional[float] = 30.0,
        max_receive_bytes: int = DEFAULT_MAX_RECEIVE_BYTES,
        namespace: str = ".",
    ) -> None:
        self._connection = QConnection(
            host,
            port,
            username=username,
            password=password,
            connect_timeout=connect_timeout,
            query_timeout=query_timeout,
            max_receive_bytes=max_receive_bytes,
        )
        # Validate namespace through the query builder without connecting.
        q_script_query("", namespace)
        self.namespace = namespace

    @property
    def connected(self) -> bool:
        return self._connection.connected

    @property
    def endpoint(self) -> str:
        host = (
            f"[{self._connection.host}]" if ":" in self._connection.host else self._connection.host
        )
        return self._connection.redact_text(f"{host}:{self._connection.port}")

    def connect(self) -> "DirectQEvaluator":
        self._connection.connect()
        return self

    def evaluate(
        self, source: str, context: Optional[EvaluationContext] = None
    ) -> EvaluationResult:
        if not isinstance(source, str):
            raise TypeError("source must be a string")
        context = context or EvaluationContext()
        if not self.connected:
            self.connect()
        value = self._connection.query(
            q_script_query(
                source,
                self.namespace,
                row_limit=context.row_limit,
                max_receive_bytes=self._connection.max_receive_bytes,
            ),
            timeout=context.timeout,
            _envelope_marker=DIRECT_Q_ENVELOPE_MARKER.encode("ascii"),
        )
        transport = _direct_q_result(
            value,
            context.row_limit,
            self._connection.redact_value,
        )
        # Never retain an unredacted decoded envelope in a frame that can raise.
        value = None
        if transport is None:
            raise EvaluatorError("invalid Direct q result envelope")
        if transport.omission is not None:
            explanation = (
                "columns exceed the portable schema limit and were omitted safely"
                if transport.omission == "columns"
                else "bounded preview exceeds the direct IPC wire limit and was omitted safely"
            )
            truncation_reason = (
                "columnLimit" if transport.omission == "columns" else "sourcePreview"
            )
            return EvaluationResult(
                QText(
                    f"[table {transport.row_count}x{transport.column_count}; {explanation}]",
                    truncated=True,
                    truncation_reasons=(truncation_reason,),
                ),
                label=f"Direct q IPC · {self.endpoint}",
            )
        value = transport.value
        if isinstance(value, (QTable, QKeyedTable)):
            if transport.row_count is None:
                raise EvaluatorError("invalid Direct q result envelope")
            if len(value.columns) > MAX_COLUMNS or any(
                not column or len(column) > 256 for column in value.columns
            ):
                return EvaluationResult(
                    QText(
                        f"[table {transport.row_count}x{len(value.columns)}; "
                        "columns exceed the portable schema limit and were omitted safely]",
                        truncated=True,
                        truncation_reasons=("columnLimit",),
                    ),
                    label=f"Direct q IPC · {self.endpoint}",
                )
            if any(not column or len(column) > 256 for column in value.columns):
                return EvaluationResult(
                    QText(
                        f"[table {transport.row_count}x{len(value.columns)}; "
                        "redacted column names exceed the portable schema limit "
                        "and were omitted safely]",
                        truncated=True,
                        truncation_reasons=("columnLimit",),
                    ),
                    label=f"Direct q IPC · {self.endpoint}",
                )
            return EvaluationResult(
                value.rows,
                columns=list(value.columns),
                row_count=transport.row_count,
                label=f"Direct q IPC · {self.endpoint}",
            )
        if isinstance(value, QText):
            text = value
        else:
            text = q_text(value)
        return EvaluationResult(
            text,
            label=f"Direct q IPC · {self.endpoint}",
        )

    def cancel(self) -> None:
        self._connection.cancel()

    def redact_text(self, value: str) -> str:
        return self._connection.redact_text(value)

    def close(self) -> None:
        self._connection.close()

    def __repr__(self) -> str:
        rendered = (
            f"{type(self).__name__}(endpoint={self.endpoint!r}, "
            f"connected={self.connected!r}, credentials=<redacted>)"
        )
        return self._connection.redact_text(rendered)


class BrokerEvaluator:
    """Authenticated JSON adapter for a future local vscode-kdb broker.

    The token is runtime-only. Redirects are rejected so authorization cannot be
    forwarded to a different endpoint.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.base_url = _broker_url(base_url)
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 8_192
            or not token.isascii()
            or not token.isprintable()
            or any(character.isspace() for character in token)
        ):
            raise ValueError("token must be a bounded printable ASCII string")
        self._token = token
        self.timeout = _positive_timeout(timeout)
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 16_384 <= max_response_bytes <= 64 * 1024 * 1024
        ):
            raise ValueError("max_response_bytes must be between 16384 and 67108864")
        self.max_response_bytes = max_response_bytes

    def evaluate(
        self, source: str, context: Optional[EvaluationContext] = None
    ) -> EvaluationResult:
        if not isinstance(source, str):
            raise TypeError("source must be a string")
        context = context or EvaluationContext()
        body: dict[str, Any] = {
            "version": 1,
            "source": source,
            "limits": {
                "rows": context.row_limit,
                "bytes": context.byte_limit,
            },
        }
        if context.timeout is not None:
            body["timeoutSeconds"] = context.timeout
        encoded = json.dumps(body, separators=(",", ":"), allow_nan=False).encode("utf-8")
        timeout = context.timeout if context.timeout is not None else self.timeout
        parsed = urllib.parse.urlsplit(self.base_url)
        connection_type = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        host = parsed.hostname
        if host is None:  # validated by _broker_url during construction
            raise EvaluatorError("broker URL lost its host")
        connection = connection_type(host, parsed.port, timeout=timeout)
        path = (parsed.path.rstrip("/") or "") + "/v1/evaluate"
        expired = threading.Event()
        transport: list[Optional[socket.socket]] = [None]
        response: Optional[http.client.HTTPResponse] = None
        raw = b""
        request_error: Optional[EvaluatorError] = None

        def expire() -> None:
            expired.set()
            active = transport[0] or connection.sock
            if active is not None:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            connection.request(
                "POST",
                path,
                body=encoded,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            transport[0] = connection.sock
            response = connection.getresponse()
            if expired.is_set():
                raise EvaluatorError("broker request timed out")
            if 300 <= response.status < 400:
                raise EvaluatorError(f"broker redirects are not allowed (HTTP {response.status})")
            if not 200 <= response.status < 300:
                raise EvaluatorError(f"broker returned HTTP {response.status}")
            content_type = (response.getheader("Content-Type") or "").split(";", 1)[0]
            if content_type.strip().lower() != "application/json":
                raise EvaluatorError("broker response must be application/json")
            response_limit = min(self.max_response_bytes, context.byte_limit)
            declared = response.getheader("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared, 10)
                except ValueError:
                    raise EvaluatorError("broker Content-Length is invalid") from None
                if declared_size < 0 or declared_size > response_limit:
                    raise EvaluatorError("broker response exceeds the configured limit")
            raw = response.read(response_limit + 1)
            if expired.is_set():
                raise EvaluatorError("broker request timed out")
            if len(raw) > response_limit:
                raise EvaluatorError("broker response exceeds the configured limit")
        except EvaluatorError as error:
            safe = str(redact_q_value(str(error), self._token))
            request_error = EvaluatorError(safe)
        except (http.client.HTTPException, OSError) as error:
            if expired.is_set():
                request_error = EvaluatorError(
                    str(redact_q_value("broker request timed out", self._token))
                )
            else:
                safe = str(redact_q_value(str(error), self._token))
                request_error = EvaluatorError(
                    str(redact_q_value(f"broker request failed: {safe}", self._token))
                )
        finally:
            timer.cancel()
            if response is not None:
                response.close()
            connection.close()
        if request_error is not None:
            raw = b""
            body.clear()
            encoded = b""
            source = self.redact_text(source)
            parsed = urllib.parse.SplitResult("", "", "", "", "")
            host = None
            path = ""
            raise request_error
        result, decode_error = _decode_broker_response(
            raw,
            row_limit=context.row_limit,
            token=self._token,
        )
        raw = b""
        if decode_error is not None:
            raise EvaluatorError(decode_error) from None
        assert result is not None
        return result

    def close(self) -> None:
        return None

    def redact_text(self, value: str) -> str:
        redacted = redact_q_value(value, self._token)
        return str(redacted)

    def __repr__(self) -> str:
        safe_url = redact_q_value(self.base_url, self._token)
        rendered = f"{type(self).__name__}(base_url={safe_url!r}, token=<redacted>)"
        return str(redact_q_value(rendered, self._token))


class PyKXEvaluator:
    """Opt-in PyKX adapter; ``pykx`` is imported only when this is selected."""

    def __init__(self, q: Optional[Callable[[str], Any]] = None) -> None:
        self._q = q

    def _resolve_q(self) -> Callable[[str], Any]:
        if self._q is None:
            try:
                import importlib

                pykx = importlib.import_module("pykx")
            except (ImportError, OSError) as error:
                raise EvaluatorError(
                    "PyKX is not installed or could not load; install the optional "
                    "PyKX dependency under its KX licensing requirements"
                ) from error
            candidate = getattr(pykx, "q", None)
            if not callable(candidate):
                raise EvaluatorError("pykx.q is unavailable or not callable")
            self._q = candidate
        if not callable(self._q):
            raise TypeError("q must be callable")
        return self._q

    def evaluate(
        self, source: str, context: Optional[EvaluationContext] = None
    ) -> EvaluationResult:
        context = context or EvaluationContext()
        value = self._resolve_q()(source)
        if all(hasattr(value, member) for member in ("__len__", "__getitem__", "py")):
            total = len(value)
            if (
                isinstance(total, bool)
                or not isinstance(total, int)
                or not 0 <= total <= JS_SAFE_INTEGER
            ):
                raise EvaluatorError("PyKX returned an invalid result length")
            preview_count = min(total, context.row_limit)
            type_name = type(value).__name__.lower()
            if "keyedtable" in type_name:
                try:
                    bounded = value.head(preview_count)
                    key_columns = bounded.keys().py()
                    value_columns = bounded.values().py()
                except Exception:
                    raise EvaluatorError(
                        "PyKX keyed table does not support bounded conversion"
                    ) from None
                if not isinstance(key_columns, Mapping) or not isinstance(value_columns, Mapping):
                    raise EvaluatorError("PyKX keyed table returned an invalid preview")
                overlap = set(key_columns) & set(value_columns)
                if overlap:
                    raise EvaluatorError("PyKX keyed table has duplicate key/value columns")
                converted_table = {**key_columns, **value_columns}
                return EvaluationResult(
                    converted_table,
                    row_count=total,
                    label="PyKX q in this Python kernel",
                )
            if "dict" in type_name:
                try:
                    keys = value.keys()[:preview_count].py()
                    values = value.values()[:preview_count].py()
                except Exception:
                    raise EvaluatorError(
                        "PyKX dictionary does not support bounded key/value conversion"
                    ) from None
                text = q_text(
                    QDictionary(keys, values),
                    max_chars=min(1_048_576, context.byte_limit),
                )
                if total > preview_count:
                    reasons = tuple(dict.fromkeys((*text.truncation_reasons, "sourcePreview")))
                    text = QText(text.text, True, reasons)
                return EvaluationResult(text, label="PyKX q in this Python kernel")
            try:
                converted = value[:preview_count].py()
            except Exception:
                raise EvaluatorError(
                    "PyKX result does not support bounded conversion; convert it "
                    "to a bounded table/vector in q first"
                ) from None
            if _pykx_table_preview(value, converted):
                return EvaluationResult(
                    converted,
                    row_count=total,
                    label="PyKX q in this Python kernel",
                )
            text = q_text(converted, max_chars=min(1_048_576, context.byte_limit))
            if total > preview_count:
                reasons = tuple(dict.fromkeys((*text.truncation_reasons, "sourcePreview")))
                text = QText(text.text, True, reasons)
            return EvaluationResult(text, label="PyKX q in this Python kernel")
        converted = value.py() if hasattr(value, "py") else value
        return EvaluationResult(q_text(converted), label="PyKX q in this Python kernel")

    def close(self) -> None:
        return None


def as_evaluator(value: EvaluatorLike) -> Evaluator:
    """Normalize a protocol object or callback."""

    if isinstance(value, Evaluator):
        return value
    if callable(value):
        return CallbackEvaluator(value)
    raise TypeError("evaluator must implement evaluate() or be callable")


@dataclass(frozen=True)
class _DirectQResult:
    value: Any
    row_count: Optional[int]
    column_count: Optional[int] = None
    omission: Optional[str] = None


def _direct_q_result(
    value: Any,
    row_limit: int,
    redact_value: Callable[[Any], Any],
) -> Optional[_DirectQResult]:
    """Validate, unwrap, and redact without leaking rejected payload locals."""

    try:
        result = _parse_direct_q_result(value, row_limit)
        safe_value = redact_value(result.value)
        return _DirectQResult(
            safe_value,
            result.row_count,
            column_count=result.column_count,
            omission=result.omission,
        )
    except Exception as error:
        # Validation is fail-closed. Clear any validator traceback that retained
        # the hostile decoded envelope, then return without propagating context.
        error.__traceback__ = None
        return None


def _parse_direct_q_result(value: Any, row_limit: int) -> _DirectQResult:
    """Strictly validate and unwrap one private DirectQEvaluator envelope."""

    if not isinstance(value, QVector) or value.ipc_type != 0 or len(value) != 4:
        raise EvaluatorError("invalid Direct q result envelope")
    marker, kind, total, payload = value
    if (
        not isinstance(marker, QCharVector)
        or marker.raw != DIRECT_Q_ENVELOPE_MARKER.encode("ascii")
        or not isinstance(kind, QSymbol)
    ):
        raise EvaluatorError("invalid Direct q result envelope")
    kind_text = kind.text()
    if kind_text == "value":
        if total is not None or isinstance(payload, (QTable, QKeyedTable)):
            raise EvaluatorError("invalid Direct q result envelope")
        return _DirectQResult(payload, None)
    if kind_text in {"tableColumns", "tableBytes"}:
        checked_total = _direct_q_total(total)
        if (
            isinstance(payload, bool)
            or not isinstance(payload, int)
            or payload < 0
            or (kind_text == "tableColumns" and payload <= MAX_COLUMNS)
            or (kind_text == "tableBytes" and payload > MAX_COLUMNS)
        ):
            raise EvaluatorError("invalid Direct q result envelope")
        return _DirectQResult(
            payload,
            checked_total,
            column_count=payload,
            omission="columns" if kind_text == "tableColumns" else "bytes",
        )
    expected_type: type[Union[QTable, QKeyedTable]]
    capped = kind_text in {"tableSafe", "keyedTableSafe"}
    if kind_text in {"table", "tableSafe"}:
        expected_type = QTable
    elif kind_text in {"keyedTable", "keyedTableSafe"}:
        expected_type = QKeyedTable
    else:
        raise EvaluatorError("invalid Direct q result envelope")
    if type(payload) is not expected_type:
        raise EvaluatorError("invalid Direct q result envelope")
    checked_total = _direct_q_total(total)
    preview_count = payload.row_count
    max_cells = DIRECT_Q_MAX_PREVIEW_CELLS // max(1, len(payload.columns))
    target_count = min(checked_total, row_limit)
    if (
        preview_count > checked_total
        or preview_count > row_limit
        or preview_count > max_cells
        or (checked_total == 0) != (preview_count == 0)
        or (not capped and preview_count != target_count)
        or (capped and not 0 < preview_count < target_count)
    ):
        raise EvaluatorError("invalid Direct q result envelope")
    return _DirectQResult(payload, checked_total)


def _direct_q_total(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= JS_SAFE_INTEGER:
        raise EvaluatorError("invalid Direct q result envelope")
    return int(value)


def _pykx_table_preview(original: Any, converted: Any) -> bool:
    type_name = type(original).__name__.lower()
    if "dict" in type_name:
        return False
    if "table" in type_name:
        return True
    if isinstance(converted, Sequence) and not isinstance(converted, (str, bytes, bytearray)):
        return bool(converted) and all(isinstance(row, Mapping) for row in converted)
    if isinstance(converted, Mapping) and converted:
        lengths: list[int] = []
        for column in converted.values():
            if isinstance(column, (str, bytes, bytearray)) or not isinstance(column, Sequence):
                return False
            lengths.append(len(column))
        return len(set(lengths)) <= 1
    return False


def _broker_result(payload: Any, *, row_limit: int) -> EvaluationResult:
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload.get("version") != 1
    ):
        raise EvaluatorError("broker response must be a version-1 object")
    label = payload.get("label")
    if label is not None and (not isinstance(label, str) or len(label) > MAX_LABEL_CHARS):
        raise EvaluatorError("broker label must be a short string")
    kind = payload.get("kind")
    if kind == "qText":
        if set(payload) - {"version", "kind", "text", "truncated", "label"}:
            raise EvaluatorError("broker qText response has unknown fields")
        text = payload.get("text")
        truncated = payload.get("truncated", False)
        if not isinstance(text, str) or not isinstance(truncated, bool):
            raise EvaluatorError("broker qText response is invalid")
        reasons = ("sourcePreview",) if truncated else ()
        return EvaluationResult(QText(text, truncated, reasons), label=label)
    if kind != "table":
        raise EvaluatorError("broker response kind must be 'table' or 'qText'")
    if set(payload) - {"version", "kind", "columns", "rows", "rowCount", "label"}:
        raise EvaluatorError("broker table response has unknown fields")
    columns, rows, row_count = (
        payload.get("columns"),
        payload.get("rows"),
        payload.get("rowCount"),
    )
    if (
        not isinstance(columns, list)
        or not all(isinstance(column, str) and column for column in columns)
        or len(columns) > MAX_COLUMNS
        or len(set(columns)) != len(columns)
        or not isinstance(rows, list)
        or len(rows) > row_limit
        or not all(isinstance(row, list) and len(row) == len(columns) for row in rows)
        or isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < len(rows)
        or row_count > JS_SAFE_INTEGER
    ):
        raise EvaluatorError("broker table response is invalid")
    return EvaluationResult(rows, columns=columns, row_count=row_count, label=label)


def _decode_broker_response(
    raw: bytes,
    *,
    row_limit: int,
    token: str,
) -> tuple[Optional[EvaluationResult], Optional[str]]:
    try:
        _guard_json_bytes(
            raw,
            max_items=min(
                500_000,
                max(10_000, row_limit * MAX_COLUMNS * 2 + 1_000),
            ),
        )
        try:
            payload = json.loads(
                raw,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_json_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "broker returned invalid JSON"
        except (RecursionError, ValueError):
            return None, "broker returned unsafe JSON"
        _validate_broker_tree(payload)
        result = _broker_result(payload, row_limit=row_limit)
        redacted = EvaluationResult(
            redact_q_value(result.value, token),
            columns=(
                None
                if result.columns is None
                else [redact_q_value(column, token) for column in result.columns]
            ),
            row_count=result.row_count,
            label=(None if result.label is None else redact_q_value(result.label, token)),
            chart=result.chart,
        )
        return redacted, None
    except EvaluatorError as error:
        message = str(redact_q_value(str(error), token))
        del error
        return None, message
    except Exception:
        return None, "broker returned an unsafe response"


def _guard_json_bytes(raw: bytes, *, max_items: int) -> None:
    depth = 0
    items = 0
    quoted = False
    escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                quoted = False
            continue
        if byte == 0x22:
            quoted = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            items += 1
            if depth > 64:
                raise EvaluatorError("broker JSON exceeds the nesting limit")
        elif byte in (0x5D, 0x7D):
            depth -= 1
        elif byte == 0x2C:
            items += 1
        if items > max_items:
            raise EvaluatorError("broker JSON exceeds the item limit")


def _reject_json_constant(value: str) -> Any:
    del value
    raise ValueError("non-finite JSON number")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _validate_broker_tree(payload: Any) -> None:
    pending: list[tuple[Any, int]] = [(payload, 0)]
    items = 0
    while pending:
        value, depth = pending.pop()
        items += 1
        if items > 500_000 or depth > 64:
            raise EvaluatorError("broker JSON exceeds safe structural limits")
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise EvaluatorError("broker JSON contains invalid Unicode") from None
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
        elif isinstance(value, dict):
            for key, item in value.items():
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError:
                    raise EvaluatorError("broker JSON contains invalid Unicode") from None
                pending.append((item, depth + 1))


def _broker_url(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("base_url must be a string")
    if not value.isascii() or any(
        ord(character) <= 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("base_url cannot contain whitespace or control characters")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("base_url contains an invalid host or port") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url must be an HTTP(S) origin/path without credentials")
    if not _loopback(parsed.hostname):
        raise ValueError("broker URLs are allowed only on loopback")
    if port is not None and port == 0:
        raise ValueError("base_url port must be between 1 and 65535")
    if parsed.path.startswith("//") or "\\" in parsed.path:
        raise ValueError("base_url contains an invalid path")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    authority = host if port is None else f"{host}:{port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), authority, parsed.path.rstrip("/"), "", "")
    )


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _positive_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("timeout must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > 86_400:
        raise ValueError("timeout must be greater than 0 and at most 86400 seconds")
    return number
