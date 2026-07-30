"""Dependency-free q IPC client and bounded decoder.

Wire behavior follows the MIT-licensed ``dreth/vscode-kdb`` direct IPC client.
This module deliberately implements plain TCP only; TLS is not advertised.
"""

from __future__ import annotations

import codecs
import datetime as dt
import math
import re
import socket
import struct
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Optional, Union, overload

from .contract import QText
from .defaults import DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_QUERY_TIMEOUT_SECONDS

HEADER_LENGTH = 8
MESSAGE_SYNC = 1
MESSAGE_RESPONSE = 2
TYPE_TABLE = 98
TYPE_DICTIONARY = 99
TYPE_ERROR = -128
INT_NULL = -2_147_483_648
INT_INFINITY = 2_147_483_647
SHORT_NULL = -32_768
SHORT_INFINITY = 32_767
Q_EPOCH = dt.datetime(2000, 1, 1)
DEFAULT_MAX_RECEIVE_BYTES = 64 * 1024 * 1024
MAX_RECEIVE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_ITEMS = 2_000_000
DEFAULT_MAX_DEPTH = 128
MAX_Q_ERROR_CHARS = 4_096
MAX_Q_SYMBOL_BYTES = 1_048_576
DIRECT_Q_ENVELOPE_MARKER = "kx-notebook/direct-q/v1"
DIRECT_Q_MAX_PREVIEW_CELLS = 1_000_000
DIRECT_Q_MAX_WIRE_BYTES = 1_000_000


class QIpcError(RuntimeError):
    """Malformed transport, handshake, connection, or protocol state."""


class QError(QIpcError):
    """Error returned by q."""


class QTimeoutError(QIpcError):
    """A connect, handshake, or query deadline elapsed."""


class QCancelledError(QIpcError):
    """The local client closed a request; server work may still continue."""


class UnsupportedQType(QIpcError):
    """Internal signal used to downgrade opaque results to bounded qText."""

    def __init__(self, q_type: int) -> None:
        self.q_type = q_type
        super().__init__(f"unsupported q IPC type {q_type}")


class QSymbol(str):
    """A q symbol preserving wire bytes when they are not valid UTF-8."""

    raw: bytes

    def __new__(cls, value: Union[str, bytes]) -> "QSymbol":
        if isinstance(value, str):
            raw = value.encode("utf-8")
            text = value
        else:
            raw = value
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = "<invalid q symbol 0x" + raw.hex() + ">"
        instance = str.__new__(cls, text)
        instance.raw = raw
        return instance

    def text(self) -> Optional[str]:
        try:
            return self.raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def __eq__(self, other: object) -> bool:
        if isinstance(other, QSymbol):
            return self.raw == other.raw
        return self.text() == other if isinstance(other, str) else False

    def __hash__(self) -> int:
        decoded = self.text()
        return hash(decoded) if decoded is not None else hash((QSymbol, self.raw))

    def __ne__(self, other: object) -> bool:
        return not self == other


class QChar(str):
    """One q char byte with a safe display form."""

    byte: int

    def __new__(cls, value: int) -> "QChar":
        text = chr(value) if 0x20 <= value <= 0x7E else f"0x{value:02x}"
        instance = str.__new__(cls, text)
        instance.byte = value
        return instance

    def __eq__(self, other: object) -> bool:
        if isinstance(other, QChar):
            return self.byte == other.byte
        return str.__eq__(self, other) if 0x20 <= self.byte <= 0x7E else False

    def __hash__(self) -> int:
        return hash(str(self)) if 0x20 <= self.byte <= 0x7E else hash((QChar, self.byte))

    def __ne__(self, other: object) -> bool:
        return not self == other


class QCharVector(Sequence[QChar]):
    """q char vector preserving wire bytes and a UTF-8 display when valid."""

    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def __len__(self) -> int:
        return len(self.raw)

    @overload
    def __getitem__(self, index: int) -> QChar: ...

    @overload
    def __getitem__(self, index: slice) -> "QCharVector": ...

    def __getitem__(self, index: Union[int, slice]) -> Union[QChar, "QCharVector"]:
        if isinstance(index, slice):
            return QCharVector(self.raw[index])
        return QChar(self.raw[index])

    def text(self) -> Optional[str]:
        try:
            return self.raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def __str__(self) -> str:
        text = self.text()
        return text if text is not None else "0x" + self.raw.hex()

    def __repr__(self) -> str:
        return f"QCharVector({str(self)!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.text() == other
        if isinstance(other, QCharVector):
            return self.raw == other.raw
        return False


class QVector(list[Any]):
    """Decoded q vector retaining its wire type for shape validation."""

    ipc_type: int

    def __init__(self, ipc_type: int, values: Sequence[Any]) -> None:
        super().__init__(values)
        self.ipc_type = ipc_type


@dataclass(frozen=True)
class QTemporal:
    """Text-preserving q temporal atom."""

    kind: str
    text: str
    __kx_temporal__: ClassVar[bool] = True

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True)
class QGeneralNull:
    """q general null ``::``."""

    def __str__(self) -> str:
        return "::"


Q_GENERAL_NULL = QGeneralNull()


@dataclass(frozen=True)
class QFunction:
    """A function whose executable internals are not exposed as Python values."""

    function_type: str
    ipc_type: int
    source: Optional[str] = None


@dataclass(frozen=True)
class QDictionary:
    """q dictionary preserving key and value vectors."""

    keys: Any
    values: Any

    @property
    def entries(self) -> list[tuple[Any, Any]]:
        keys = _as_list(self.keys)
        values = _as_list(self.values)
        if len(keys) != len(values):
            raise QIpcError("q dictionary contains unequal key and value counts")
        return list(zip(keys, values))


class _TableRows(Sequence[dict[str, Any]]):
    def __init__(self, table: "QTable") -> None:
        self._table = table

    def __len__(self) -> int:
        return self._table.row_count

    @overload
    def __getitem__(self, index: int) -> dict[str, Any]: ...

    @overload
    def __getitem__(self, index: slice) -> list[dict[str, Any]]: ...

    def __getitem__(self, index: Union[int, slice]) -> Union[dict[str, Any], list[dict[str, Any]]]:
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return {
            name: self._table.column_data[column][index]
            for column, name in enumerate(self._table.columns)
        }

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for index in range(len(self)):
            yield self[index]


@dataclass
class QTable:
    """Strict columnar q table with lazy row mappings."""

    columns: list[str]
    column_data: list[Sequence[Any]]
    row_count: int

    def __post_init__(self) -> None:
        if len(self.columns) != len(self.column_data):
            raise QIpcError("q table column names and values have unequal counts")
        lengths = [len(column) for column in self.column_data]
        if lengths and any(length != lengths[0] for length in lengths):
            raise QIpcError("q table contains unequal column lengths")
        expected = lengths[0] if lengths else 0
        if self.row_count != expected:
            raise QIpcError("q table row count is inconsistent")
        if len(set(self.columns)) != len(self.columns):
            raise QIpcError("q table contains duplicate column names")

    @property
    def rows(self) -> Sequence[dict[str, Any]]:
        return _TableRows(self)


@dataclass
class QKeyedTable:
    """q keyed table with key columns followed by value columns."""

    key_table: QTable
    value_table: QTable

    def __post_init__(self) -> None:
        if self.key_table.row_count != self.value_table.row_count:
            raise QIpcError("key and value tables contain unequal row counts")
        if set(self.key_table.columns) & set(self.value_table.columns):
            raise QIpcError("keyed table contains duplicate key/value columns")

    @property
    def columns(self) -> list[str]:
        return [*self.key_table.columns, *self.value_table.columns]

    @property
    def column_data(self) -> list[Sequence[Any]]:
        return [*self.key_table.column_data, *self.value_table.column_data]

    @property
    def row_count(self) -> int:
        return self.key_table.row_count

    @property
    def rows(self) -> Sequence[dict[str, Any]]:
        table = QTable(self.columns, self.column_data, self.row_count)
        return table.rows


QValue = Any


def serialize_text_query(source: str) -> bytes:
    """Serialize q source as a synchronous char-vector request."""

    if not isinstance(source, str):
        raise TypeError("source must be a string")
    encoded = source.encode("utf-8")
    payload = struct.pack("<bBi", 10, 0, len(encoded)) + encoded
    return struct.pack("<BBBBi", 1, MESSAGE_SYNC, 0, 0, HEADER_LENGTH + len(payload)) + payload


def deserialize_message(
    message: bytes,
    *,
    max_receive_bytes: int = DEFAULT_MAX_RECEIVE_BYTES,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    redactions: Sequence[str] = (),
    deadline: Optional[float] = None,
    envelope_marker: Optional[bytes] = None,
) -> QValue:
    """Decode one complete q IPC response.

    Opaque value types become an explicit bounded ``QText`` summary. Malformed
    encodings still raise: silently guessing would invent values.
    """

    _receive_limit(max_receive_bytes)
    if (
        isinstance(max_items, bool)
        or not isinstance(max_items, int)
        or not 1 <= max_items <= 10_000_000
    ):
        raise ValueError("max_items must be between 1 and 10000000")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 0 <= max_depth <= 256:
        raise ValueError("max_depth must be between 0 and 256")
    _check_deadline(deadline)
    if len(message) < HEADER_LENGTH:
        raise QIpcError("invalid q IPC message: incomplete header")
    little = _message_little_endian(message)
    declared = _int32(message, 4, little)
    if declared != len(message):
        raise QIpcError(f"invalid q IPC message length {declared} for {len(message)} bytes")
    if declared > max_receive_bytes:
        raise QIpcError("q IPC message exceeds the configured receive limit")
    if message[1] != MESSAGE_RESPONSE:
        raise QIpcError(f"unexpected q IPC message kind {message[1]}")
    if message[2] not in (0, 1):
        raise QIpcError(f"invalid q IPC compression flag {message[2]}")
    if message[3] != 0:
        raise QIpcError(f"invalid q IPC reserved header byte {message[3]}")
    normalized = (
        _decompress_message(message, max_receive_bytes, deadline=deadline)
        if message[2] == 1
        else message
    )
    _check_deadline(deadline)
    little = _message_little_endian(normalized)
    reader = _QReader(
        normalized[HEADER_LENGTH:],
        little,
        max_items=max_items,
        max_depth=max_depth,
        redactions=redactions,
        deadline=deadline,
    )
    try:
        value = reader.read_payload(envelope_marker=envelope_marker)
        _check_deadline(deadline)
        return value
    except UnsupportedQType as error:
        if envelope_marker is not None:
            raise QIpcError("invalid q IPC result envelope") from None
        omitted = len(normalized) - HEADER_LENGTH
        return QText(
            f"[unsupported q IPC type {error.q_type}; {omitted} payload bytes omitted safely]",
            truncated=True,
            truncation_reasons=("sourcePreview",),
        )


def q_script_groups(script: str) -> list[str]:
    """Mirror legacy q physical script grouping without using ``.Q.ld``."""

    lines = script.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[0].startswith("#!"):
        lines.pop(0)
    groups: list[str] = []
    current: Optional[list[str]] = None
    pending: list[str] = []
    block_depth = 0

    def flush_current() -> None:
        nonlocal current
        if current is not None:
            groups.append("\n".join(current))
            current = None

    def flush_pending() -> None:
        nonlocal pending
        groups.extend(pending)
        pending = []

    for original in lines:
        leading = re.match(r"^[ \t]*", original)
        prefix = leading.group(0) if leading else ""
        line = prefix.replace("\t", " ") + original[len(prefix) :]
        if block_depth:
            if re.fullmatch(r"/[ \t]*", line):
                groups.append("/")
                block_depth += 1
            elif re.fullmatch(r"\\[ \t]*", line):
                groups.append("/")
                block_depth -= 1
            else:
                groups.append("/" if not line.strip() else "/" + line)
            continue
        if re.fullmatch(r"/[ \t]*", line):
            flush_current()
            flush_pending()
            groups.append("/")
            block_depth = 1
            continue
        if re.fullmatch(r"\\[ \t]*", line):
            flush_current()
            flush_pending()
            break
        blank = not line.strip()
        if blank or line.startswith("/"):
            pending.append("" if blank else line)
            continue
        if line[0].isspace():
            if current is not None:
                current.extend([*pending, line])
            pending = []
            continue
        flush_current()
        flush_pending()
        current = [line]
    flush_current()
    flush_pending()
    return groups


def q_script_query(
    script: str,
    namespace: str = ".",
    *,
    row_limit: Optional[int] = None,
    max_receive_bytes: int = DEFAULT_MAX_RECEIVE_BYTES,
) -> str:
    """Build the legacy-compatible complete-cell ``value`` reduction.

    When ``row_limit`` is supplied, the result is wrapped once in the private
    DirectQEvaluator envelope. Tables are counted and bounded in q before IPC
    serialization; other values are carried unchanged inside the envelope.
    """

    if namespace != "." and not re.fullmatch(
        r"\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", namespace
    ):
        raise ValueError("namespace must be '.' or dot-separated q identifiers")
    if row_limit is not None and (
        isinstance(row_limit, bool)
        or not isinstance(row_limit, int)
        or not 1 <= row_limit <= 10_000
    ):
        raise ValueError("row_limit must be between 1 and 10000")
    receive_limit = _receive_limit(max_receive_bytes)
    groups = q_script_groups(script)
    while groups and not groups[-1].strip():
        groups.pop()
    if not groups:
        encoded_groups = "()"
    elif len(groups) == 1:
        encoded_groups = "enlist " + _q_string(groups[0])
    else:
        encoded_groups = "(" + ";".join(_q_string(group) for group in groups) + ")"
    cell_query = (
        "{[ns;groups]\n"
        '  previous:string system "d";\n'
        '  system "d ",ns;\n'
        "  outcome:@[{[groups]\n"
        "    result:$[count groups;{[unused;expression]\n"
        "      value $[-10h=type expression;enlist expression;expression]\n"
        "    }/[::;groups];::];\n"
        "    (1b;result)\n"
        "  };groups;{(0b;x)}];\n"
        '  system "d ",previous;\n'
        "  if[not first outcome;'last outcome];\n"
        "  last outcome\n"
        f"}}[{_q_string(namespace)};{encoded_groups}]"
    )
    if row_limit is None:
        return cell_query
    # q's ``-8!`` includes the complete eight-byte IPC message header.
    wire_limit = min(receive_limit, DIRECT_Q_MAX_WIRE_BYTES)
    return (
        "{[result;limit;wireLimit;cellLimit]\n"
        "  kind:$[98h=type result;`table;"
        "$[99h=type result;"
        "$[98 98h~type each (key result;value result);`keyedTable;`value];"
        "`value]];\n"
        f"  if[`value=kind;:({_q_string(DIRECT_Q_ENVELOPE_MARKER)};"
        "kind;0Nj;result)];\n"
        "  total:count result;\n"
        "  columnCount:count cols result;\n"
        f"  if[{256}<columnCount;:({_q_string(DIRECT_Q_ENVELOPE_MARKER)};"
        "`tableColumns;total;columnCount)];\n"
        "  targetCount:limit&total;\n"
        "  previewCount:targetCount&cellLimit div 1|columnCount;\n"
        "  safeKind:$[`table=kind;`tableSafe;`keyedTableSafe];\n"
        "  previewKind:$[previewCount<targetCount;safeKind;kind];\n"
        "  preview:previewCount#result;\n"
        f"  envelope:({_q_string(DIRECT_Q_ENVELOPE_MARKER)};"
        "previewKind;total;preview);\n"
        "  while[(wireLimit<count -8!envelope)&0<previewCount;\n"
        "    previewCount:previewCount div 2;\n"
        "    previewKind:safeKind;\n"
        "    preview:previewCount#result;\n"
        f"    envelope:({_q_string(DIRECT_Q_ENVELOPE_MARKER)};"
        "previewKind;total;preview)];\n"
        "  if[(wireLimit<count -8!envelope)|((0<total)&0=previewCount);"
        f":({_q_string(DIRECT_Q_ENVELOPE_MARKER)};"
        "`tableBytes;total;columnCount)];\n"
        "  envelope\n"
        f"}}[{cell_query};{row_limit};{wire_limit};{DIRECT_Q_MAX_PREVIEW_CELLS}]"
    )


class QConnection:
    """One plain-TCP q IPC session supporting one synchronous query at a time."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        username: str = "",
        password: Optional[str] = None,
        connect_timeout: Optional[float] = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        query_timeout: Optional[float] = DEFAULT_QUERY_TIMEOUT_SECONDS,
        max_receive_bytes: int = DEFAULT_MAX_RECEIVE_BYTES,
    ) -> None:
        if (
            not isinstance(host, str)
            or not host
            or len(host) > 1_024
            or not host.isprintable()
            or "\x00" in host
        ):
            raise ValueError("host must be a non-empty string")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
            raise ValueError("port must be between 1 and 65535")
        if (
            not isinstance(username, str)
            or (password is not None and not isinstance(password, str))
            or len(username) > 1_024
            or (password is not None and len(password) > 4_096)
            or any(character in username for character in ("\x00", "\r", "\n", ":"))
            or (password is not None and "\x00" in password)
        ):
            raise ValueError(
                "username must not contain colons/newlines; credentials cannot contain NUL bytes"
            )
        try:
            username.encode("utf-8")
            if password is not None:
                password.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("credentials must contain valid UTF-8 text") from None
        self.host = host
        self.port = port
        self.username = username
        self._password = password or ""
        self.connect_timeout = _timeout("connect_timeout", connect_timeout)
        self.query_timeout = _timeout("query_timeout", query_timeout)
        self.max_receive_bytes = _receive_limit(max_receive_bytes)
        self._socket: Optional[socket.socket] = None
        self._connecting_socket: Optional[socket.socket] = None
        self._generation = 0
        self._state_lock = threading.Lock()
        self._connect_lock = threading.Lock()
        self._query_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._socket is not None

    def __repr__(self) -> str:
        safe_host = _redact(self.host, self._password)
        safe_username = _redact(self.username, self._password)
        rendered = (
            f"{type(self).__name__}(host={safe_host!r}, port={self.port!r}, "
            f"username={safe_username!r}, password=<redacted>, "
            f"connected={self.connected!r})"
        )
        return _redact(rendered, self._password)

    def connect(self) -> "QConnection":
        with self._connect_lock:
            with self._state_lock:
                if self._socket is not None:
                    return self
                generation = self._generation
            endpoint = _redact(f"{self.host}:{self.port}", self._password)
            connection: Optional[socket.socket] = None
            connect_deadline = (
                None if self.connect_timeout is None else time.monotonic() + self.connect_timeout
            )
            pending_error: Optional[BaseException] = None
            try:
                connection = socket.create_connection(
                    (self.host, self.port), timeout=self.connect_timeout
                )
                with self._state_lock:
                    if generation != self._generation:
                        raise QCancelledError("q IPC connection was canceled locally")
                    self._connecting_socket = connection
                handshake_timeout = (
                    None if connect_deadline is None else connect_deadline - time.monotonic()
                )
                if handshake_timeout is not None and handshake_timeout <= 0:
                    raise socket.timeout("q IPC connect deadline elapsed")
                connection.settimeout(handshake_timeout)
                handshake = bytearray(
                    (
                        f"{self.username}:{self._password}"
                        if self.username or self._password
                        else ""
                    ).encode("utf-8")
                )
                handshake.extend((3, 0))
                try:
                    connection.sendall(handshake)
                finally:
                    handshake[:] = b"\x00" * len(handshake)
                    del handshake
                response = _receive_exact(connection, 1, deadline=connect_deadline)
                if not response or not 1 <= response[0] <= 3:
                    raise QIpcError("q rejected IPC authentication")
                connection.settimeout(self.query_timeout)
                with self._state_lock:
                    if generation != self._generation or self._connecting_socket is not connection:
                        raise QCancelledError("q IPC connection was canceled locally")
                    self._connecting_socket = None
                    self._socket = connection
                return self
            except BaseException as error:
                with self._state_lock:
                    canceled = generation != self._generation
                    if self._connecting_socket is connection:
                        self._connecting_socket = None
                _shutdown(connection)
                if isinstance(error, KeyboardInterrupt):
                    raise
                if canceled:
                    pending_error = QCancelledError("q IPC connection was canceled locally")
                elif isinstance(error, socket.timeout):
                    pending_error = QTimeoutError(
                        f"q IPC connect/handshake to {endpoint} timed out"
                    )
                elif isinstance(error, QIpcError):
                    safe = _redact(str(error), self.username, self._password)
                    error_type = type(error)
                    pending_error = error_type(safe)
                elif isinstance(error, OSError):
                    safe = _redact(str(error), self.username, self._password)
                    pending_error = QIpcError(f"q IPC connection to {endpoint} failed: {safe}")
                else:
                    raise
            if pending_error is not None:
                raise pending_error
            raise AssertionError("q IPC connection failed without an error")

    def query(
        self,
        source: str,
        *,
        timeout: Optional[float] = None,
        _envelope_marker: Optional[bytes] = None,
    ) -> QValue:
        """Issue one sync request. A timeout/interrupt closes the session."""

        with self._query_lock:
            with self._state_lock:
                if self._socket is None:
                    raise QIpcError("q IPC connection is not open")
                connection = self._socket
                generation = self._generation
            query_timeout = self.query_timeout if timeout is None else _timeout("timeout", timeout)
            deadline = None if query_timeout is None else time.monotonic() + query_timeout
            pending_error: Optional[QIpcError] = None
            header = b""
            message = b""
            try:
                connection.settimeout(query_timeout)
                connection.sendall(serialize_text_query(source))
                header = _receive_exact(connection, HEADER_LENGTH, deadline=deadline)
                little = _message_little_endian(header)
                length = _int32(header, 4, little)
                if length < HEADER_LENGTH:
                    raise QIpcError(f"invalid q IPC message length {length}")
                if length > self.max_receive_bytes:
                    raise QIpcError("q IPC message exceeds the configured receive limit")
                message = header + _receive_exact(
                    connection,
                    length - HEADER_LENGTH,
                    deadline=deadline,
                )
                value = deserialize_message(
                    message,
                    max_receive_bytes=self.max_receive_bytes,
                    redactions=(self.username, self._password),
                    deadline=deadline,
                    envelope_marker=_envelope_marker,
                )
                connection.settimeout(self.query_timeout)
                return value
            except socket.timeout:
                self._discard(connection)
                pending_error = QTimeoutError(
                    "q IPC query timed out; connection closed because server work may continue"
                )
            except KeyboardInterrupt:
                self._discard(connection)
                raise
            except QError as error:
                safe = _redact(str(error), self.username, self._password)
                pending_error = QError(safe)
            except (QIpcError, OSError) as error:
                with self._state_lock:
                    canceled = generation != self._generation and self._socket is not connection
                self._discard(connection)
                if canceled:
                    pending_error = QCancelledError("q IPC query was canceled locally")
                else:
                    safe = _redact(str(error), self.username, self._password)
                    pending_error = (
                        QIpcError(safe)
                        if isinstance(error, QIpcError)
                        else QIpcError(f"q IPC query transport failed: {safe}")
                    )
            if pending_error is not None:
                # Keep raw server/query bytes out of verbose traceback-local renderers.
                source = self.redact_text(source)
                header = b""
                message = b""
                raise pending_error
            raise AssertionError("q IPC query failed without an error")

    def cancel(self) -> None:
        """Close locally; already-issued q work may continue on the server."""

        self.close()

    def redact_value(self, value: QValue) -> QValue:
        """Remove configured password text before a value leaves the adapter."""

        return redact_q_value(value, self._password)

    def redact_text(self, value: str) -> str:
        """Remove configured password text from persisted provenance."""

        return _redact(value, self._password)

    def close(self) -> None:
        with self._state_lock:
            self._generation += 1
            connection, self._socket = self._socket, None
            connecting, self._connecting_socket = self._connecting_socket, None
        _shutdown(connection)
        if connecting is not connection:
            _shutdown(connecting)

    def _discard(self, connection: socket.socket) -> None:
        with self._state_lock:
            if self._socket is connection:
                self._socket = None
                self._generation += 1
        _shutdown(connection)

    def __enter__(self) -> "QConnection":
        return self.connect()

    def __exit__(self, *_: object) -> None:
        self.close()


class _QReader:
    def __init__(
        self,
        data: bytes,
        little_endian: bool,
        *,
        max_items: int,
        max_depth: int,
        redactions: Sequence[str],
        deadline: Optional[float],
    ) -> None:
        self.data = data
        self.little = little_endian
        self.position = 0
        self.max_items = max_items
        self.max_depth = max_depth
        self.redactions = tuple(item for item in redactions if item)
        self.deadline = deadline
        self.items = 0
        self._next_deadline_item = 0

    def read_payload(self, *, envelope_marker: Optional[bytes] = None) -> QValue:
        _check_deadline(self.deadline)
        value = (
            self._read_enveloped_payload(envelope_marker)
            if envelope_marker is not None
            else self.read_object(0)
        )
        if self.position != len(self.data):
            raise QIpcError(
                f"invalid q IPC payload: {len(self.data) - self.position} trailing bytes"
            )
        return value

    def _read_enveloped_payload(self, marker_bytes: bytes) -> QVector:
        self._count(1)
        q_type = self.i8()
        if q_type == TYPE_ERROR:
            message = self.error_symbol()
            if self.position != len(self.data):
                raise QIpcError("invalid q error payload: trailing bytes")
            raise QError(message)
        if q_type != 0 or self.u8() != 0 or self.i32() != 4:
            raise QIpcError("invalid q IPC result envelope")
        marker = self.read_object(1)
        if not isinstance(marker, QCharVector) or marker.raw != marker_bytes:
            raise QIpcError("invalid q IPC result envelope")
        kind = self.read_object(1)
        total = self.read_object(1)
        payload = self.read_object(1)
        return QVector(0, [marker, kind, total, payload])

    def read_object(self, depth: int) -> QValue:
        if depth > self.max_depth:
            raise QIpcError("q IPC payload exceeds the nesting limit")
        self._count(1)
        q_type = self.i8()
        if q_type == TYPE_ERROR:
            message = self.error_symbol()
            if self.position != len(self.data):
                raise QIpcError("invalid q error payload: trailing bytes")
            raise QError(message)
        if -20 < q_type < 0:
            return self.atom(-q_type)
        if q_type == TYPE_TABLE:
            return self.table(depth)
        if q_type == TYPE_DICTIONARY:
            return self.dictionary(depth)
        if 100 <= q_type <= 112:
            return self.function(q_type, depth)
        if not 0 <= q_type <= 19:
            raise UnsupportedQType(q_type)
        attribute = self.u8()
        if attribute > 4:
            raise QIpcError(f"invalid q vector attribute {attribute}")
        length = self.i32()
        self._vector_length(length)
        if q_type == 10:
            self._count(length)
            value = QCharVector(self.raw(length))
            _check_deadline(self.deadline)
            return value
        values: list[Any] = []
        if q_type != 0:
            self._count(length)
        for index in range(length):
            if index % 4_096 == 0:
                _check_deadline(self.deadline)
            values.append(self.read_object(depth + 1) if q_type == 0 else self.atom(q_type))
        return QVector(q_type, values)

    def atom(self, q_type: int) -> QValue:
        if q_type == 1:
            value = self.i8()
            if value not in (0, 1):
                raise QIpcError(f"invalid q boolean byte {value}")
            return value == 1
        if q_type == 2:
            raw = self.raw(16)
            return None if not any(raw) else str(uuid.UUID(bytes=raw))
        if q_type == 4:
            return self.u8()
        if q_type == 5:
            return _nullable_int(self.i16(), SHORT_NULL, SHORT_INFINITY)
        if q_type == 6:
            return _nullable_int(self.i32(), INT_NULL, INT_INFINITY)
        if q_type == 7:
            value = self.i64()
            if value == -(1 << 63):
                return None
            if value == (1 << 63) - 1:
                return math.inf
            if value == -(1 << 63) + 1:
                return -math.inf
            return value
        if q_type == 8:
            return _nullable_float(self.f32())
        if q_type == 9:
            return _nullable_float(self.f64())
        if q_type == 10:
            return QChar(self.u8())
        if q_type == 11:
            return self.symbol()
        if q_type == 12:
            value = self.i64()
            special = _nullable_long(value)
            return special if special is not value else QTemporal("timestamp", _timestamp(value))
        if q_type == 13:
            value = _nullable_int(self.i32(), INT_NULL, INT_INFINITY)
            return _temporal_or_special("month", value, _month)
        if q_type == 14:
            value = _nullable_int(self.i32(), INT_NULL, INT_INFINITY)
            return _temporal_or_special("date", value, _date)
        if q_type == 15:
            value = _nullable_float(self.f64())
            return _temporal_or_special("datetime", value, _datetime)
        if q_type == 16:
            value = self.i64()
            special = _nullable_long(value)
            return special if special is not value else QTemporal("timespan", _timespan(value))
        if q_type == 17:
            value = _nullable_int(self.i32(), INT_NULL, INT_INFINITY)
            return _temporal_or_special("minute", value, _minute)
        if q_type == 18:
            value = _nullable_int(self.i32(), INT_NULL, INT_INFINITY)
            return _temporal_or_special("second", value, _second)
        if q_type == 19:
            value = _nullable_int(self.i32(), INT_NULL, INT_INFINITY)
            return _temporal_or_special("time", value, _time)
        raise UnsupportedQType(q_type)

    def table(self, depth: int) -> QTable:
        attribute = self.u8()
        if attribute > 4:
            raise QIpcError(f"invalid q table attribute {attribute}")
        if self.i8() != TYPE_DICTIONARY:
            raise QIpcError("invalid q table: expected dictionary payload")
        columns = self.read_object(depth + 1)
        data = self.read_object(depth + 1)
        if not isinstance(columns, QVector) or columns.ipc_type != 11:
            raise QIpcError("invalid q table: column names must be a symbol vector")
        if not isinstance(data, QVector) or data.ipc_type != 0:
            raise QIpcError("invalid q table: column data must be a general list")
        names = [_column_name(item, index) for index, item in enumerate(columns)]
        vectors = list(data)
        if len(names) != len(vectors):
            raise QIpcError("q table column names and data have unequal counts")
        typed_vectors: list[Sequence[Any]] = []
        for vector in vectors:
            if isinstance(vector, QCharVector):
                typed_vectors.append(vector)
            elif isinstance(vector, QVector):
                typed_vectors.append(vector)
            else:
                raise QIpcError("q table column data must contain vectors")
        row_count = len(typed_vectors[0]) if typed_vectors else 0
        return QTable(names, typed_vectors, row_count)

    def dictionary(self, depth: int) -> Union[QDictionary, QKeyedTable]:
        keys = self.read_object(depth + 1)
        values = self.read_object(depth + 1)
        if isinstance(keys, QTable) != isinstance(values, QTable):
            raise QIpcError("invalid q dictionary: keyed table sides must both be tables")
        if isinstance(keys, QTable) and isinstance(values, QTable):
            return QKeyedTable(keys, values)
        if _value_count(keys) != _value_count(values):
            raise QIpcError("q dictionary contains unequal key and value counts")
        result = QDictionary(keys, values)
        return result

    def function(self, q_type: int, depth: int) -> QValue:
        if q_type == 100:
            self.symbol()
            payload = self.read_object(depth + 1)
            source = (
                payload.text()
                if isinstance(payload, QCharVector) and len(payload) <= MAX_Q_SYMBOL_BYTES
                else payload
                if isinstance(payload, str)
                else None
            )
            return QFunction("lambda", q_type, source)
        if q_type < 104:
            opcode = self.i8()
            if q_type == 101 and opcode == 0:
                return Q_GENERAL_NULL
            return QFunction(_function_name(q_type), q_type)
        if q_type > 105:
            self.read_object(depth + 1)
        else:
            length = self.i32()
            self._vector_length(length)
            self._count(length)
            for _ in range(length):
                self.read_object(depth + 1)
        return QFunction(_function_name(q_type), q_type)

    def symbol(self) -> Optional[QSymbol]:
        _check_deadline(self.deadline)
        end = self.data.find(b"\x00", self.position)
        _check_deadline(self.deadline)
        if end < 0:
            raise QIpcError("invalid q symbol: missing terminator")
        length = end - self.position
        if length > MAX_Q_SYMBOL_BYTES:
            self.position = end + 1
            raise QIpcError("q symbol exceeds the supported byte limit")
        raw = self.data[self.position : end]
        self.position = end + 1
        return QSymbol(raw) if raw else None

    def error_symbol(self) -> str:
        _check_deadline(self.deadline)
        end = self.data.find(b"\x00", self.position)
        _check_deadline(self.deadline)
        if end < 0:
            raise QIpcError("invalid q error: missing terminator")
        overlap = max(
            (len(secret.encode("utf-8")) for secret in self.redactions),
            default=0,
        )
        prefix_end = min(end, self.position + MAX_Q_ERROR_CHARS * 4 + overlap)
        raw = self.data[self.position : prefix_end]
        self.position = end + 1
        message = _redact(
            raw.decode("utf-8", errors="replace"),
            *self.redactions,
        )
        return _bounded_error(message or "q error", truncated=prefix_end < end)

    def raw(self, length: int) -> bytes:
        self.ensure(length)
        result = self.data[self.position : self.position + length]
        self.position += length
        return result

    def i8(self) -> int:
        return int(self._unpack("b", 1))

    def u8(self) -> int:
        return int(self._unpack("B", 1))

    def i16(self) -> int:
        return int(self._unpack("h", 2))

    def i32(self) -> int:
        return int(self._unpack("i", 4))

    def i64(self) -> int:
        return int(self._unpack("q", 8))

    def f32(self) -> float:
        return float(self._unpack("f", 4))

    def f64(self) -> float:
        return float(self._unpack("d", 8))

    def _unpack(self, code: str, size: int) -> Any:
        self.ensure(size)
        value = struct.unpack_from(("<" if self.little else ">") + code, self.data, self.position)[
            0
        ]
        self.position += size
        return value

    def ensure(self, length: int) -> None:
        if length < 0 or self.position + length > len(self.data):
            raise QIpcError("invalid q IPC payload: unexpected end")

    def _count(self, count: int) -> None:
        self.items += count
        if self.items > self.max_items:
            raise QIpcError("q IPC payload exceeds the item limit")
        if self.items >= self._next_deadline_item:
            _check_deadline(self.deadline)
            self._next_deadline_item = self.items + 4_096

    def _vector_length(self, length: int) -> None:
        if length < 0:
            raise QIpcError(f"invalid q IPC vector length {length}")
        if length > self.max_items:
            raise QIpcError("q IPC vector exceeds the item limit")


def q_text(value: QValue, *, max_chars: int = 1_048_576) -> QText:
    """Format a decoded non-table q value without claiming unsupported detail."""

    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or not 1 <= max_chars <= 1_048_576
    ):
        raise ValueError("max_chars must be between 1 and 1048576")
    state = [max_chars, False]
    reasons: set[str] = set()

    def mark(reason: str = "cellValueLimit") -> None:
        state[1] = True
        reasons.add(reason)

    def take(text: str) -> str:
        remaining = state[0]
        if remaining <= 0:
            mark()
            return ""
        if len(text) > remaining:
            mark()
            text = text[:remaining]
        state[0] -= len(text)
        return text

    def render(item: Any, depth: int = 0) -> str:
        if depth > 32:
            mark()
            return take("...")
        if item is Q_GENERAL_NULL or isinstance(item, QGeneralNull):
            return take("::")
        if item is None:
            return take("0N")
        if isinstance(item, bool):
            return take("1b" if item else "0b")
        if isinstance(item, QChar):
            if 0x20 <= item.byte <= 0x7E:
                escaped = str(item).replace("\\", "\\\\").replace('"', '\\"')
                return take('"' + escaped + '"')
            return take(f'"\\{item.byte:03o}"')
        if isinstance(item, QSymbol):
            decoded = item.text()
            if decoded is not None:
                return take("`" + decoded)
            prefix_size = max(0, (state[0] - len("[symbol bytes 0x]")) // 2)
            symbol_prefix = item.raw[:prefix_size].hex()
            if prefix_size < len(item.raw):
                mark()
            return take(f"[symbol bytes 0x{symbol_prefix}]")
        if isinstance(item, QCharVector):
            if not _valid_utf8(item.raw):
                prefix_size = max(0, (state[0] - 2) // 2)
                hex_prefix = item.raw[:prefix_size].hex()
                if prefix_size < len(item.raw):
                    mark()
                return take("0x" + hex_prefix)
            raw_prefix = item.raw[: state[0]]
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            decoded = decoder.decode(raw_prefix, final=False)
            escaped = (
                decoded.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\r", "\\r")
                .replace("\n", "\\n")
                .replace("\t", "\\t")
                .replace("\x00", "\\000")
            )
            if len(raw_prefix) < len(item.raw):
                mark()
            return take('"' + escaped + '"')
        if isinstance(item, QTemporal):
            return take(item.text)
        if isinstance(item, str):
            escaped = item.replace("\\", "\\\\").replace('"', '\\"')
            return take('"' + escaped + '"')
        if isinstance(item, QFunction):
            mark("sourcePreview")
            if item.source:
                return take(item.source)
            return take(f"[{item.function_type}: source unavailable over q IPC]")
        if isinstance(item, QDictionary):
            return render(item.keys, depth + 1) + take("!") + render(item.values, depth + 1)
        if isinstance(item, QTable):
            mark("sourcePreview")
            return take(f"[table {item.row_count}x{len(item.columns)}]")
        if isinstance(item, QKeyedTable):
            mark("sourcePreview")
            return take(f"[keyed table {item.row_count}x{len(item.columns)}]")
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            simple = len(item) <= 256 and all(
                isinstance(child, (int, float, bool, QSymbol, QTemporal)) or child is None
                for child in item
            )
            separator = " " if simple else ";"
            parts = [take("(")]
            for index, child in enumerate(item):
                if state[0] <= 0:
                    mark()
                    break
                if index:
                    parts.append(take(separator))
                parts.append(render(child, depth + 1))
            parts.append(take(")"))
            return "".join(parts)
        return take(str(item))

    rendered = render(value)
    if state[1]:
        suffix = "... [truncated]"
        if max_chars <= len(suffix):
            rendered = suffix[:max_chars]
        else:
            rendered = rendered[: max_chars - len(suffix)] + suffix
    return QText(
        rendered,
        truncated=bool(state[1]),
        truncation_reasons=tuple(
            reason for reason in ("sourcePreview", "cellValueLimit") if reason in reasons
        ),
    )


def redact_q_value(value: QValue, *secrets: str) -> QValue:
    """Recursively redact known runtime secrets from decoded/output values."""

    active = tuple(secret for secret in secrets if secret)
    if not active:
        return value
    if isinstance(value, QCharVector):
        raw = value.raw
        marker = _redaction_byte(active)
        for secret in sorted(active, key=len, reverse=True):
            encoded = secret.encode("utf-8")
            raw = raw.replace(encoded, bytes((marker,)) * len(encoded))
        return QCharVector(raw)
    if isinstance(value, QSymbol):
        raw = value.raw
        marker = _redaction_byte(active)
        for secret in sorted(active, key=len, reverse=True):
            encoded = secret.encode("utf-8")
            raw = raw.replace(encoded, bytes((marker,)) * len(encoded))
        return QSymbol(raw)
    if isinstance(value, str):
        return _redact(value, *active)
    if isinstance(value, QText):
        return QText(
            _redact(value.text, *active),
            value.truncated,
            value.truncation_reasons,
        )
    if isinstance(value, QTemporal):
        return QTemporal(value.kind, _redact(value.text, *active))
    if isinstance(value, QFunction):
        return QFunction(
            value.function_type,
            value.ipc_type,
            None if value.source is None else _redact(value.source, *active),
        )
    if isinstance(value, QTable):
        return QTable(
            _redacted_unique_names(value.columns, active),
            [redact_q_value(column, *active) for column in value.column_data],
            value.row_count,
        )
    if isinstance(value, QKeyedTable):
        key_count = len(value.key_table.columns)
        names = _redacted_unique_names(value.columns, active)
        return QKeyedTable(
            QTable(
                names[:key_count],
                [redact_q_value(column, *active) for column in value.key_table.column_data],
                value.row_count,
            ),
            QTable(
                names[key_count:],
                [redact_q_value(column, *active) for column in value.value_table.column_data],
                value.row_count,
            ),
        )
    if isinstance(value, QDictionary):
        return QDictionary(
            redact_q_value(value.keys, *active),
            redact_q_value(value.values, *active),
        )
    if isinstance(value, list):
        return [redact_q_value(item, *active) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_q_value(item, *active) for item in value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        names = _redacted_unique_names([str(key) for key in value], active)
        for candidate, item in zip(names, value.values()):
            result[candidate] = redact_q_value(item, *active)
        return result
    if isinstance(value, bytes):
        binary = value
        marker = _redaction_byte(active)
        for secret in active:
            encoded = secret.encode("utf-8")
            binary = binary.replace(encoded, bytes((marker,)) * len(encoded))
        return binary
    return value


def _receive_exact(
    connection: socket.socket,
    length: int,
    *,
    deadline: Optional[float] = None,
) -> bytes:
    output = bytearray(length)
    view = memoryview(output)
    offset = 0
    remaining = length
    while remaining:
        if deadline is not None:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise socket.timeout("q IPC query deadline elapsed")
            connection.settimeout(timeout)
        received = connection.recv_into(view[offset:], remaining)
        if not received:
            raise QIpcError("q IPC connection closed while receiving a message")
        offset += received
        remaining -= received
    return bytes(output)


def _shutdown(connection: Optional[socket.socket]) -> None:
    if connection is None:
        return
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    connection.close()


def _message_little_endian(message: bytes) -> bool:
    if not message or message[0] not in (0, 1):
        value = message[0] if message else "<missing>"
        raise QIpcError(f"invalid q IPC endian flag {value}")
    return message[0] == 1


def _int32(data: bytes, offset: int, little: bool) -> int:
    if len(data) < offset + 4:
        raise QIpcError("invalid q IPC message: incomplete header")
    return int(struct.unpack_from("<i" if little else ">i", data, offset)[0])


def _decompress_message(
    message: bytes,
    max_receive_bytes: int,
    *,
    deadline: Optional[float],
) -> bytes:
    if len(message) < 12:
        raise QIpcError("invalid compressed q IPC message: incomplete header")
    little = _message_little_endian(message)
    output_length = _int32(message, 8, little)
    if not HEADER_LENGTH <= output_length <= max_receive_bytes:
        raise QIpcError("invalid or oversized compressed q IPC length")
    compressed_payload_bytes = len(message) - 12
    if output_length - HEADER_LENGTH > compressed_payload_bytes * 257:
        raise QIpcError("compressed q IPC expansion is impossible for the wire length")
    output = bytearray(bytes((message[0], message[1], 0, message[3])))
    output.extend(struct.pack("<i" if little else ">i", output_length))
    lookup = [0] * 256
    source = 12
    destination = HEADER_LENGTH
    pending = HEADER_LENGTH
    flags = 0
    mask = 0
    next_deadline_check = destination
    while destination < output_length:
        if destination >= next_deadline_check:
            _check_deadline(deadline)
            next_deadline_check = destination + 16_384
        if mask == 0:
            if source >= len(message):
                raise QIpcError("truncated q IPC compression flags")
            flags = message[source]
            source += 1
            mask = 1
        if flags & mask:
            if source + 2 > len(message):
                raise QIpcError("truncated q IPC back-reference")
            reference = lookup[message[source]]
            count = message[source + 1]
            source += 2
            if (
                reference < HEADER_LENGTH
                or reference + 1 >= destination
                or reference + 2 + count > output_length
                or destination + 2 + count > output_length
            ):
                raise QIpcError("invalid q IPC back-reference")
            output.extend(output[reference : reference + 2])
            destination += 2
            for index in range(count):
                output.append(output[reference + 2 + index])
            while pending < destination - 1:
                lookup[output[pending] ^ output[pending + 1]] = pending
                pending += 1
            destination += count
            pending = destination
        else:
            if source >= len(message):
                raise QIpcError("truncated q IPC compressed literal")
            output.append(message[source])
            source += 1
            destination += 1
            while pending < destination - 1:
                lookup[output[pending] ^ output[pending + 1]] = pending
                pending += 1
        mask <<= 1
        if mask == 256:
            mask = 0
    if source != len(message):
        raise QIpcError("compressed q IPC message contains trailing bytes")
    _check_deadline(deadline)
    return bytes(output)


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, QSymbol):
        return [value]
    if isinstance(value, str):
        return list(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _value_count(value: Any) -> int:
    if isinstance(value, QSymbol):
        return 1
    if isinstance(value, str):
        return len(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return len(value)
    return 1


def _column_name(value: Any, index: int) -> str:
    del index
    if value is None:
        raise QIpcError("q table contains a null column name")
    name = value if isinstance(value, QSymbol) else str(value)
    if not name:
        raise QIpcError("q table contains an empty column name")
    return name


def _nullable_int(value: int, null: int, infinity: int) -> Any:
    if value == null:
        return None
    if value == infinity:
        return math.inf
    if value == -infinity:
        return -math.inf
    return value


def _nullable_float(value: float) -> Any:
    return None if math.isnan(value) else value


def _nullable_long(value: int) -> Any:
    if value == -(1 << 63):
        return None
    if value == (1 << 63) - 1:
        return math.inf
    if value == -(1 << 63) + 1:
        return -math.inf
    return value


def _timestamp(nanoseconds: int) -> str:
    seconds, nanos = divmod(nanoseconds, 1_000_000_000)
    moment = Q_EPOCH + dt.timedelta(seconds=seconds)
    return moment.strftime("%Y.%m.%dD%H:%M:%S.") + f"{nanos:09d}"


def _month(value: Union[int, float]) -> str:
    raw = int(value)
    year, month = divmod(raw, 12)
    return f"{2000 + year:04d}.{month + 1:02d}m"


def _date(value: Union[int, float]) -> str:
    return (Q_EPOCH + dt.timedelta(days=float(value))).strftime("%Y.%m.%d")


def _datetime(value: Union[int, float]) -> str:
    return (Q_EPOCH + dt.timedelta(days=float(value))).strftime("%Y.%m.%dT%H:%M:%S.%f")[:-3]


def _timespan(value: int) -> str:
    sign = "-" if value < 0 else ""
    raw = abs(value)
    days, raw = divmod(raw, 86_400_000_000_000)
    hours, raw = divmod(raw, 3_600_000_000_000)
    minutes, raw = divmod(raw, 60_000_000_000)
    seconds, nanos = divmod(raw, 1_000_000_000)
    return f"{sign}{days}D{hours:02d}:{minutes:02d}:{seconds:02d}.{nanos:09d}"


def _minute(value: Union[int, float]) -> str:
    raw = int(value)
    sign = "-" if raw < 0 else ""
    hours, minutes = divmod(abs(raw), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def _second(value: Union[int, float]) -> str:
    raw = int(value)
    sign = "-" if raw < 0 else ""
    raw = abs(raw)
    hours, raw = divmod(raw, 3_600)
    minutes, seconds = divmod(raw, 60)
    return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}"


def _time(value: Union[int, float]) -> str:
    raw = int(value)
    sign = "-" if raw < 0 else ""
    raw = abs(raw)
    hours, raw = divmod(raw, 3_600_000)
    minutes, raw = divmod(raw, 60_000)
    seconds, millis = divmod(raw, 1_000)
    return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _temporal_or_special(kind: str, value: Any, formatter: Any) -> Any:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return value
    try:
        return QTemporal(kind, formatter(value))
    except (OverflowError, ValueError, OSError):
        raise QIpcError(f"q {kind} atom is outside the supported range") from None


def _function_name(q_type: int) -> str:
    return {
        100: "lambda",
        101: "primitive",
        102: "operator",
        103: "iterator",
        104: "projection",
        105: "composition",
    }.get(q_type, "function")


def _q_string(value: str) -> str:
    return (
        '"'
        + value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        + '"'
    )


def _timeout(name: str, value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > 86_400:
        raise ValueError(f"{name} must be greater than 0 and at most 86400 seconds")
    return number


def _receive_limit(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1_024 <= value <= MAX_RECEIVE_BYTES
    ):
        raise ValueError(f"max_receive_bytes must be between 1024 and {MAX_RECEIVE_BYTES}")
    return value


def _valid_utf8(raw: bytes) -> bool:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        for offset in range(0, len(raw), 65_536):
            decoder.decode(raw[offset : offset + 65_536], final=False)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def _check_deadline(deadline: Optional[float]) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise socket.timeout("q IPC query deadline elapsed")


def _redact(message: str, *secrets: str) -> str:
    safe = message
    active = sorted({item for item in secrets if item}, key=len, reverse=True)
    marker = _redaction_marker(active)
    for secret in active:
        safe = safe.replace(secret, marker)
    return safe


def _redaction_marker(secrets: Sequence[str]) -> str:
    for candidate in ("█", "◆", "●", "¤", "§"):
        if all(candidate not in secret for secret in secrets):
            return candidate
    for codepoint in range(0xE000, 0xF900):
        candidate = chr(codepoint)
        if all(candidate not in secret for secret in secrets):
            return candidate
    return "\ufffd"


def _redaction_byte(secrets: Sequence[str]) -> int:
    del secrets
    # Valid UTF-8 credentials can never contain 0xff, including across matches.
    return 0xFF


def _redacted_unique_names(names: Sequence[str], secrets: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    marker = _redaction_marker(secrets)
    for name in names:
        base = _redact(str(name), *secrets)
        candidate = base
        collision = 1
        while candidate in seen:
            # The separator itself is absent from every bounded credential, so
            # suffixing cannot recreate a secret at a string boundary.
            candidate = base + marker * collision
            collision += 1
        result.append(candidate)
        seen.add(candidate)
    return result


def _bounded_error(message: str, *, truncated: bool = False) -> str:
    suffix = "… [truncated]"
    parts: list[str] = []
    size = 0
    for character in message:
        piece = (
            character
            if character in {"\n", "\t"} or character.isprintable()
            else f"\\x{ord(character):02x}"
        )
        reserve = len(suffix) if truncated or size + len(piece) > MAX_Q_ERROR_CHARS else 0
        if size + len(piece) + reserve > MAX_Q_ERROR_CHARS:
            truncated = True
            break
        parts.append(piece)
        size += len(piece)
    text = "".join(parts)
    if truncated:
        text = text[: MAX_Q_ERROR_CHARS - len(suffix)] + suffix
    return text
