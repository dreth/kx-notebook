"""Small deterministic q IPC fixture encoder and scripted loopback server."""

from __future__ import annotations

import socket
import struct
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

DIRECT_Q_ENVELOPE_MARKER = "kx-notebook/direct-q/v1"


def q_message(payload: bytes, *, message_type: int = 2, little_endian: bool = True) -> bytes:
    byte_order = "<" if little_endian else ">"
    endian_flag = 1 if little_endian else 0
    header = bytes((endian_flag, message_type, 0, 0))
    return header + struct.pack(f"{byte_order}i", 8 + len(payload)) + payload


def q_error(message: str) -> bytes:
    return struct.pack("b", -128) + message.encode("utf-8") + b"\0"


def q_bool(value: bool) -> bytes:
    return struct.pack("bb", -1, int(value))


def q_byte(value: int) -> bytes:
    return struct.pack("bB", -4, value)


def q_short(value: int) -> bytes:
    return struct.pack("<bh", -5, value)


def q_int(value: int) -> bytes:
    return struct.pack("<bi", -6, value)


def q_long(value: int) -> bytes:
    return struct.pack("<bq", -7, value)


def q_real(value: float) -> bytes:
    return struct.pack("<bf", -8, value)


def q_float(value: float) -> bytes:
    return struct.pack("<bd", -9, value)


def q_char(value: str) -> bytes:
    encoded = value.encode("latin-1")
    if len(encoded) != 1:
        raise ValueError("q char fixtures require exactly one Latin-1 byte")
    return struct.pack("b", -10) + encoded


def q_symbol(value: str) -> bytes:
    return struct.pack("b", -11) + value.encode("utf-8") + b"\0"


def q_timestamp(nanoseconds_since_2000: int) -> bytes:
    return struct.pack("<bq", -12, nanoseconds_since_2000)


def q_month(months_since_2000: int) -> bytes:
    return struct.pack("<bi", -13, months_since_2000)


def q_date(days_since_2000: int) -> bytes:
    return struct.pack("<bi", -14, days_since_2000)


def q_datetime(days_since_2000: float) -> bytes:
    return struct.pack("<bd", -15, days_since_2000)


def q_timespan(nanoseconds: int) -> bytes:
    return struct.pack("<bq", -16, nanoseconds)


def q_minute(minutes: int) -> bytes:
    return struct.pack("<bi", -17, minutes)


def q_second(seconds: int) -> bytes:
    return struct.pack("<bi", -18, seconds)


def q_time(milliseconds: int) -> bytes:
    return struct.pack("<bi", -19, milliseconds)


def q_general_list(items: Sequence[bytes]) -> bytes:
    return bytes((0, 0)) + struct.pack("<i", len(items)) + b"".join(items)


def q_vector(type_code: int, raw_items: Sequence[bytes]) -> bytes:
    return bytes((type_code, 0)) + struct.pack("<i", len(raw_items)) + b"".join(raw_items)


def q_bool_vector(values: Sequence[bool]) -> bytes:
    return q_vector(1, [struct.pack("b", int(value)) for value in values])


def q_int_vector(values: Sequence[int]) -> bytes:
    return q_vector(6, [struct.pack("<i", value) for value in values])


def q_long_vector(values: Sequence[int]) -> bytes:
    return q_vector(7, [struct.pack("<q", value) for value in values])


def q_float_vector(values: Sequence[float]) -> bytes:
    return q_vector(9, [struct.pack("<d", value) for value in values])


def q_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return bytes((10, 0)) + struct.pack("<i", len(encoded)) + encoded


def q_symbol_vector(values: Sequence[str]) -> bytes:
    return q_vector(11, [value.encode("utf-8") + b"\0" for value in values])


def q_dictionary(keys: bytes, values: bytes) -> bytes:
    return bytes((99,)) + keys + values


def q_table(columns: Mapping[str, bytes]) -> bytes:
    return (
        bytes((98, 0, 99)) + q_symbol_vector(list(columns)) + q_general_list(list(columns.values()))
    )


def q_keyed_table(
    key_columns: Mapping[str, bytes],
    value_columns: Mapping[str, bytes],
) -> bytes:
    return q_dictionary(q_table(key_columns), q_table(value_columns))


def q_direct_result(value: bytes, *, kind: str, row_count: int | None = None) -> bytes:
    """Encode the private DirectQEvaluator response envelope."""

    encoded_count = q_long(-9_223_372_036_854_775_808 if row_count is None else row_count)
    return q_general_list(
        [
            q_string(DIRECT_Q_ENVELOPE_MARKER),
            q_symbol(kind),
            encoded_count,
            value,
        ]
    )


def request_source(message: bytes) -> str:
    """Decode the one request shape emitted by ``serialize_text_query``."""

    if len(message) < 14 or message[1] != 1 or message[8] != 10:
        raise ValueError("not a synchronous q char-vector request")
    byte_order = "<" if message[0] == 1 else ">"
    declared = struct.unpack_from(f"{byte_order}i", message, 4)[0]
    text_length = struct.unpack_from(f"{byte_order}i", message, 10)[0]
    if declared != len(message) or 14 + text_length != len(message):
        raise ValueError("invalid request length")
    return message[14:].decode("utf-8")


@dataclass(frozen=True)
class Exchange:
    """One response after one complete client query."""

    response: bytes | None
    delay: float = 0.0
    chunk_sizes: tuple[int, ...] = ()


class ScriptedQServer:
    """Minimal q-like server for handshake/query lifecycle tests."""

    def __init__(
        self,
        exchanges: Sequence[Exchange],
        *,
        handshake_version: int = 3,
        handshake_delay: float = 0.0,
    ) -> None:
        self._exchanges = list(exchanges)
        self._handshake_version = handshake_version
        self._handshake_delay = handshake_delay
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._listener.settimeout(0.1)
        self.host, self.port = self._listener.getsockname()[:2]
        self._connection: socket.socket | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._handshake = b""
        self.requests: list[bytes] = []
        self.sources: list[str] = []
        self.errors: list[BaseException] = []

    def __enter__(self) -> ScriptedQServer:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def authenticated_as(self, username: str, password: str) -> bool:
        expected = f"{username}:{password}".encode()
        return self._handshake.startswith(expected) and self._handshake.endswith(b"\x03\0")

    def close(self) -> None:
        self._stop.set()
        connection = self._connection
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self._listener.close()
        self._thread.join(timeout=2)

    def _serve(self) -> None:
        try:
            connection = self._accept()
            if connection is None:
                return
            self._connection = connection
            connection.settimeout(0.1)
            self._handshake = self._read_until_nul(connection)
            if self._handshake_delay:
                self._stop.wait(self._handshake_delay)
            connection.sendall(bytes((self._handshake_version,)))
            if self._handshake_version < 1:
                return
            for exchange in self._exchanges:
                request = self._read_message(connection)
                if request is None:
                    return
                self.requests.append(request)
                self.sources.append(request_source(request))
                if exchange.delay:
                    self._stop.wait(exchange.delay)
                if exchange.response is None or self._stop.is_set():
                    return
                self._send_chunks(connection, exchange.response, exchange.chunk_sizes)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Client timeout/cancel tests intentionally tear down the socket.
            return
        except BaseException as error:  # pragma: no cover - surfaced by assertions
            self.errors.append(error)

    def _accept(self) -> socket.socket | None:
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
                return connection
            except socket.timeout:
                continue
            except OSError:
                return None
        return None

    def _read_until_nul(self, connection: socket.socket) -> bytes:
        value = bytearray()
        while not self._stop.is_set():
            try:
                chunk = connection.recv(1)
            except socket.timeout:
                continue
            if not chunk:
                break
            value.extend(chunk)
            if chunk == b"\0":
                break
        return bytes(value)

    def _read_message(self, connection: socket.socket) -> bytes | None:
        header = self._read_exact(connection, 8)
        if header is None:
            return None
        byte_order = "<" if header[0] == 1 else ">"
        length = struct.unpack_from(f"{byte_order}i", header, 4)[0]
        if length < 8:
            raise ValueError(f"invalid request length {length}")
        body = self._read_exact(connection, length - 8)
        return None if body is None else header + body

    def _read_exact(self, connection: socket.socket, length: int) -> bytes | None:
        value = bytearray()
        while len(value) < length and not self._stop.is_set():
            try:
                chunk = connection.recv(length - len(value))
            except socket.timeout:
                continue
            if not chunk:
                return None
            value.extend(chunk)
        return bytes(value) if len(value) == length else None

    @staticmethod
    def _send_chunks(
        connection: socket.socket,
        response: bytes,
        chunk_sizes: tuple[int, ...],
    ) -> None:
        offset = 0
        for size in chunk_sizes:
            if offset >= len(response):
                break
            end = min(len(response), offset + size)
            connection.sendall(response[offset:end])
            offset = end
            time.sleep(0.001)
        if offset < len(response):
            connection.sendall(response[offset:])
