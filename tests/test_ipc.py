from __future__ import annotations

import math
import socket
import struct
import threading
import time
from typing import Any

import pytest

from kx_notebook.ipc import (
    DIRECT_Q_ENVELOPE_MARKER,
    QCancelledError,
    QChar,
    QCharVector,
    QConnection,
    QDictionary,
    QError,
    QIpcError,
    QKeyedTable,
    QSymbol,
    QTable,
    QText,
    QTimeoutError,
    deserialize_message,
    q_script_groups,
    q_script_query,
    q_text,
    redact_q_value,
    serialize_text_query,
)

from .qipc_fixtures import (
    Exchange,
    ScriptedQServer,
    q_bool,
    q_bool_vector,
    q_byte,
    q_char,
    q_date,
    q_datetime,
    q_dictionary,
    q_direct_result,
    q_error,
    q_float,
    q_float_vector,
    q_general_list,
    q_int,
    q_int_vector,
    q_keyed_table,
    q_long,
    q_long_vector,
    q_message,
    q_minute,
    q_month,
    q_real,
    q_second,
    q_short,
    q_string,
    q_symbol,
    q_symbol_vector,
    q_table,
    q_time,
    q_timespan,
    q_timestamp,
)


def package_traceback_locals(error: BaseException) -> str:
    frames: list[str] = []
    current = error.__traceback__
    while current is not None:
        if "/src/kx_notebook/" in current.tb_frame.f_code.co_filename:
            frames.append(repr(current.tb_frame.f_locals))
        current = current.tb_next
    return "\n".join(frames)


def test_serialize_text_query_matches_q_sync_char_vector_wire_format() -> None:
    source = "select from trade where sym=`AAPL"
    message = serialize_text_query(source)
    encoded = source.encode()

    assert message[:4] == b"\x01\x01\x00\x00"
    assert struct.unpack_from("<i", message, 4)[0] == len(message)
    assert message[8:10] == b"\x0a\x00"
    assert struct.unpack_from("<i", message, 10)[0] == len(encoded)
    assert message[14:] == encoded


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": "", "port": 5000},
        {"host": "localhost", "port": 0},
        {"host": "localhost", "port": 65_536},
        {"host": "localhost", "port": 5000, "username": "alice:other"},
        {"host": "localhost", "port": 5000, "username": "alice\nother"},
        {"host": "localhost", "port": 5000, "password": "bad\0password"},
        {"host": "localhost", "port": 5000, "connect_timeout": True},
        {"host": "localhost", "port": 5000, "query_timeout": "5"},
    ],
)
def test_connection_options_reject_ambiguous_auth_and_invalid_endpoint(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        QConnection(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("a:1\nb:2\na+b", ["a:1", "b:2", "a+b"]),
        (
            "#!/usr/bin/env q\nlegacyShebang:1\nlegacyShebang",
            ["legacyShebang:1", "legacyShebang"],
        ),
        (
            "legacyFn:{[x]\r\n x+1\r\n }\r\nlegacyFn 4",
            ["legacyFn:{[x]\n x+1\n }", "legacyFn 4"],
        ),
        (
            "legacyTrade:([]sym:`A`B;size:10 20)\nselect from legacyTrade where\n size>10",
            [
                "legacyTrade:([]sym:`A`B;size:10 20)",
                "select from legacyTrade where\n size>10",
            ],
        ),
        (
            "legacyCtl:0\nif[1b;\n legacyCtl:7;\n legacyCtl+:1]\nlegacyCtl",
            [
                "legacyCtl:0",
                "if[1b;\n legacyCtl:7;\n legacyCtl+:1]",
                "legacyCtl",
            ],
        ),
        (
            "legacyTabFn:{[x]\n\tvalue:x+1;\n\tvalue*2\n\t}\nlegacyTabFn 4",
            [
                "legacyTabFn:{[x]\n value:x+1;\n value*2\n }",
                "legacyTabFn 4",
            ],
        ),
        (
            "/ heading\na:1\n\n/ between\nb:2\n",
            ["/ heading", "a:1", "", "/ between", "b:2", ""],
        ),
        (
            "/\nnot executable\n2+2\n\\\na:1",
            ["/", "/not executable", "/2+2", "/", "a:1"],
        ),
        (
            "a:1\n/\n/\nnot executable\n\\\nstill not executable\n\\\na+:1\na",
            [
                "a:1",
                "/",
                "/",
                "/not executable",
                "/",
                "/still not executable",
                "/",
                "a+:1",
                "a",
            ],
        ),
        ("beforeStop:1\n\\   \nafterStop:2", ["beforeStop:1"]),
        ("  ignored:1\nactual:2", ["actual:2"]),
        ("  ignored:1", []),
    ],
)
def test_legacy_compatible_q_script_grouping(
    source: str,
    expected: list[str],
) -> None:
    assert q_script_groups(source) == expected


def test_complete_source_wrapper_uses_no_dot_q_ld_and_restores_namespace() -> None:
    query = q_script_query("a:1\r\nb:2\r\na+b", ".analytics")

    assert ".Q.ld" not in query
    assert "value $[-10h=type expression;enlist expression;expression]" in query
    assert '"a:1";"b:2";"a+b"' in query
    assert '".analytics"' in query
    assert 'previous:string system "d"' in query
    assert 'system "d ",previous' in query
    assert "if[not first outcome;'last outcome]" in query
    with pytest.raises(ValueError, match="namespace"):
        q_script_query("1+1", ".bad-name")


def test_complete_source_wrapper_ignores_ipython_trailing_blank_lines() -> None:
    query = q_script_query("([]x:1 2)\n\n")

    assert '"([]x:1 2)"' in query
    assert ';""' not in query


def test_bounded_complete_source_wrapper_composes_one_server_side_table_preview() -> None:
    query = q_script_query(
        "sideEffect+:1\n([]x:til 500000)",
        ".analytics",
        row_limit=3,
        max_receive_bytes=4_096,
    )

    assert query.count('"sideEffect+:1"') == 1
    assert query.count('"([]x:til 500000)"') == 1
    assert DIRECT_Q_ENVELOPE_MARKER in query
    assert "total:count result" in query
    assert "preview:previewCount#result" in query
    assert "`keyedTable" in query
    # q's ``-8!`` includes the complete eight-byte IPC message header.
    assert "4096" in query
    assert 'system "d ",previous' in query
    assert "if[not first outcome;'last outcome]" in query


@pytest.mark.parametrize("row_limit", [0, True, 10_001])
def test_bounded_complete_source_wrapper_validates_row_limit(row_limit: Any) -> None:
    with pytest.raises(ValueError, match="row_limit"):
        q_script_query("1+1", row_limit=row_limit)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (q_bool(True), True),
        (q_byte(255), 255),
        (q_short(-123), -123),
        (q_int(42), 42),
        (q_long(2**62), 2**62),
        (q_real(1.5), 1.5),
        (q_float(2.25), 2.25),
        (q_char("x"), "x"),
        (q_symbol("AAPL"), "AAPL"),
        (q_string("hello λ"), "hello λ"),
        (q_bool_vector([True, False]), [True, False]),
        (q_int_vector([1, 2, 3]), [1, 2, 3]),
        (q_symbol_vector(["AAPL", "MSFT"]), ["AAPL", "MSFT"]),
    ],
)
def test_deserialize_common_atoms_and_vectors(payload: bytes, expected: Any) -> None:
    assert deserialize_message(q_message(payload)) == expected


def test_q_char_vectors_preserve_wire_bytes_and_table_row_cardinality() -> None:
    value = deserialize_message(q_message(q_string("λ")))
    assert isinstance(value, QCharVector)
    assert value.raw == "λ".encode()
    assert value.text() == "λ"

    table = deserialize_message(
        q_message(q_table({"byte": q_string("ab"), "n": q_int_vector([1, 2])}))
    )
    assert isinstance(table, QTable)
    assert table.row_count == 2
    assert [str(row["byte"]) for row in table.rows] == ["a", "b"]

    invalid = deserialize_message(q_message(bytes((10, 0)) + struct.pack("<i", 2) + b"\xff\xfe"))
    assert isinstance(invalid, QCharVector)
    assert invalid.text() is None
    assert str(invalid) == "0xfffe"


def test_non_ascii_char_and_invalid_symbol_render_without_inventing_text() -> None:
    char = deserialize_message(q_message(bytes((246, 255))))
    assert isinstance(char, QChar)
    assert q_text(char).text == '"\\377"'

    symbol = deserialize_message(q_message(bytes((245, 255, 0))))
    assert str(symbol) == "<invalid q symbol 0xff>"
    assert symbol.text() is None
    assert "symbol bytes 0xff" in q_text(symbol).text
    assert "\ufffd" not in q_text(symbol).text
    assert symbol != str(symbol)
    assert symbol != deserialize_message(q_message(q_symbol(str(symbol))))


def test_qtext_bounds_large_char_vectors_before_hex_or_escape_expansion() -> None:
    invalid = QCharVector(b"\xff" * 2_000_000)
    rendered = q_text(invalid, max_chars=16)

    assert len(rendered.text) <= 16
    assert rendered.truncated is True


def test_char_vectors_count_toward_the_aggregate_item_limit() -> None:
    payload = q_general_list([q_string("abcdefghij"), q_string("klmnopqrst")])

    with pytest.raises(QIpcError, match="item limit"):
        deserialize_message(q_message(payload), max_items=15)


def test_table_wire_shape_rejects_scalar_columns_and_non_symbol_names() -> None:
    scalar_column = (
        bytes((98, 0, 99)) + q_symbol_vector(["x"]) + q_general_list([q_symbol("not-a-vector")])
    )
    with pytest.raises(QIpcError, match="vectors"):
        deserialize_message(q_message(scalar_column))

    general_names = (
        bytes((98, 0, 99)) + q_general_list([q_symbol("x")]) + q_general_list([q_int_vector([1])])
    )
    with pytest.raises(QIpcError, match="symbol vector"):
        deserialize_message(q_message(general_names))


def test_malformed_compression_cannot_copy_header_or_hide_trailing_bytes() -> None:
    # Literal q int type (-6), followed by an unseen back-reference. A zeroed
    # lookup table must never let malformed input copy bytes from the IPC header.
    compressed = bytes((0b10, 0xFA, 0xFF, 2))
    message = (
        bytes((1, 2, 1, 0))
        + struct.pack("<i", 12 + len(compressed))
        + struct.pack("<i", 13)
        + compressed
    )
    with pytest.raises(QIpcError, match="back-reference"):
        deserialize_message(message)

    # A valid all-literal compressed q int must consume the complete wire body.
    literal = bytes((0,)) + q_int(42)
    valid = (
        bytes((1, 2, 1, 0)) + struct.pack("<i", 12 + len(literal)) + struct.pack("<i", 13) + literal
    )
    assert deserialize_message(valid) == 42
    with_trailing = valid + b"junk"
    with_trailing = with_trailing[:4] + struct.pack("<i", len(with_trailing)) + with_trailing[8:]
    with pytest.raises(QIpcError, match="trailing"):
        deserialize_message(with_trailing)

    impossible = bytes((1, 2, 1, 0)) + struct.pack("<i", 12) + struct.pack("<i", 64 * 1024 * 1024)
    with pytest.raises(QIpcError, match="expansion|wire"):
        deserialize_message(impossible)


@pytest.mark.parametrize("kind", ["value", "table"])
@pytest.mark.parametrize("compressed", [False, True])
def test_direct_envelope_rejects_unsupported_payload_and_trailing_bytes(
    kind: str,
    compressed: bool,
) -> None:
    payload = q_direct_result(
        bytes((20,)) + b"unvalidated-trailing-bytes",
        kind=kind,
        row_count=None if kind == "value" else 1,
    )
    if compressed:
        literal_body = b"".join(
            bytes((0,)) + payload[offset : offset + 8] for offset in range(0, len(payload), 8)
        )
        message = (
            bytes((1, 2, 1, 0))
            + struct.pack("<i", 12 + len(literal_body))
            + struct.pack("<i", 8 + len(payload))
            + literal_body
        )
    else:
        message = q_message(payload)

    with pytest.raises(QIpcError, match="envelope"):
        deserialize_message(
            message,
            envelope_marker=DIRECT_Q_ENVELOPE_MARKER.encode("ascii"),
        )


def test_malformed_attributes_headers_atoms_and_error_tails_fail_closed() -> None:
    invalid_reserved = bytearray(q_message(q_int(1)))
    invalid_reserved[3] = 255
    with pytest.raises(QIpcError, match="reserved"):
        deserialize_message(bytes(invalid_reserved))

    invalid_vector_attribute = q_message(bytes((6, 255)) + struct.pack("<i", 1) + b"\0" * 4)
    with pytest.raises(QIpcError, match="attribute"):
        deserialize_message(invalid_vector_attribute)

    invalid_table_attribute = q_message(
        bytes((98, 255, 99)) + q_symbol_vector(["x"]) + q_general_list([q_int_vector([1])])
    )
    with pytest.raises(QIpcError, match="attribute"):
        deserialize_message(invalid_table_attribute)

    with pytest.raises(QIpcError, match="boolean"):
        deserialize_message(q_message(bytes((255, 2))))
    with pytest.raises(QIpcError, match="trailing"):
        deserialize_message(q_message(q_error("type") + b"junk"))


def test_keyed_table_dictionary_requires_two_table_sides() -> None:
    one_table = q_dictionary(
        q_table({"key": q_symbol_vector(["a"])}),
        q_int_vector([1]),
    )

    with pytest.raises(QIpcError, match="both|sides"):
        deserialize_message(q_message(one_table))


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (q_int(-(2**31)), None),
        (q_int(2**31 - 1), math.inf),
        (q_int(-(2**31 - 1)), -math.inf),
        (q_long(-(2**63)), None),
        (q_symbol(""), None),
    ],
)
def test_q_null_and_infinity_sentinels(payload: bytes, expected: Any) -> None:
    actual = deserialize_message(q_message(payload))
    if isinstance(expected, float) and math.isinf(expected):
        assert actual == expected
    else:
        assert actual is expected


def test_deserialize_general_list_dictionary_and_unkeyed_table() -> None:
    listed = deserialize_message(
        q_message(q_general_list([q_int(7), q_symbol("AAPL"), q_bool(False)]))
    )
    assert listed == [7, "AAPL", False]

    dictionary = deserialize_message(
        q_message(
            q_dictionary(
                q_symbol_vector(["venue", "count"]),
                q_general_list([q_symbol("XNYS"), q_long(2)]),
            )
        )
    )
    assert isinstance(dictionary, QDictionary)
    assert dictionary.keys == ["venue", "count"]
    assert dictionary.values == ["XNYS", 2]

    table = deserialize_message(
        q_message(
            q_table(
                {
                    "sym": q_symbol_vector(["AAPL", "MSFT"]),
                    "price": q_float_vector([224.1, 518.0]),
                    "size": q_long_vector([100, 200]),
                }
            )
        )
    )
    assert isinstance(table, QTable)
    assert list(table.columns) == ["sym", "price", "size"]
    assert table.row_count == 2
    assert list(table.rows) == [
        {"sym": "AAPL", "price": 224.1, "size": 100},
        {"sym": "MSFT", "price": 518.0, "size": 200},
    ]


def test_deserialize_keyed_table_preserves_keys_and_rows() -> None:
    value = deserialize_message(
        q_message(
            q_keyed_table(
                {"sym": q_symbol_vector(["AAPL", "MSFT"])},
                {
                    "price": q_float_vector([224.1, 518.0]),
                    "size": q_long_vector([100, 200]),
                },
            )
        )
    )

    assert isinstance(value, QKeyedTable)
    assert list(value.columns) == ["sym", "price", "size"]
    assert value.row_count == 2
    assert list(value.rows) == [
        {"sym": "AAPL", "price": 224.1, "size": 100},
        {"sym": "MSFT", "price": 518.0, "size": 200},
    ]


def test_temporal_atoms_decode_to_unambiguous_portable_values() -> None:
    values = deserialize_message(
        q_message(
            q_general_list(
                [
                    q_timestamp(0),
                    q_month(0),
                    q_date(0),
                    q_datetime(0),
                    q_timespan(90_000_000_000),
                    q_minute(61),
                    q_second(3661),
                    q_time(3_661_250),
                ]
            )
        )
    )
    rendered = [str(value) for value in values]

    assert all("2000" in value for value in rendered[:4])
    assert "00:01:30" in rendered[4]
    assert rendered[5].startswith("01:01")
    assert rendered[6].startswith("01:01:01")
    assert rendered[7].startswith("01:01:01")


def test_negative_clock_temporals_keep_a_single_leading_sign() -> None:
    values = deserialize_message(
        q_message(q_general_list([q_minute(-1), q_second(-1), q_time(-1)]))
    )

    assert [str(value) for value in values] == [
        "-00:01",
        "-00:00:01",
        "-00:00:00.001",
    ]


def test_big_endian_response_and_fragmented_network_response_are_supported() -> None:
    big_endian_int = q_message(
        struct.pack(">bi", -6, 42),
        little_endian=False,
    )
    with ScriptedQServer([Exchange(big_endian_int, chunk_sizes=(1, 2, 3, 4))]) as server:
        with QConnection(server.host, server.port) as connection:
            assert connection.query("6*7") == 42

    assert server.sources == ["6*7"]
    assert server.errors == []


def test_q_errors_surface_as_q_errors() -> None:
    with pytest.raises(QError, match="type"):
        deserialize_message(q_message(q_error("type")))


@pytest.mark.parametrize(
    "message",
    [
        b"",
        b"\x01\x02\x00\x00\x08\x00\x00",
        b"\x02\x02\x00\x00\x08\x00\x00\x00",
        b"\x01\x02\x00\x00\x09\x00\x00\x00",
        q_message(q_int(1)) + b"trailing",
    ],
)
def test_malformed_messages_fail_closed(message: bytes) -> None:
    with pytest.raises(QIpcError):
        deserialize_message(message)


def test_unsupported_value_is_a_bounded_qtext_value() -> None:
    value = deserialize_message(q_message(bytes((20,))))

    assert isinstance(value, QText)
    assert value.text
    assert len(value.text) <= 4096
    assert isinstance(deserialize_message(q_message(bytes((127,)))), QText)


def test_function_placeholder_discloses_that_source_is_unavailable() -> None:
    function = deserialize_message(q_message(bytes((101, 1))))
    rendered = q_text(function)

    assert rendered.truncated is True
    assert "sourcePreview" in rendered.truncation_reasons
    assert "source unavailable" in rendered.text


def test_public_decoder_limits_reject_unsafe_types_and_qtext_stays_bounded() -> None:
    message = q_message(q_int(1))
    for kwargs in (
        {"max_items": True},
        {"max_items": 10_000_001},
        {"max_depth": True},
        {"max_depth": 257},
        {"max_receive_bytes": 64 * 1024 * 1024 + 1},
    ):
        with pytest.raises(ValueError):
            deserialize_message(message, **kwargs)

    assert len(q_text([1, 2], max_chars=1).text) == 1


def test_connection_authenticates_queries_and_closes_idempotently() -> None:
    fake_password = "fixture-password"
    with ScriptedQServer([Exchange(q_message(q_int(42)))]) as server:
        connection = QConnection(
            server.host,
            server.port,
            username="alice",
            password=fake_password,
        )
        assert fake_password not in repr(connection)
        connection.connect()
        assert connection.query("6*7") == 42
        connection.close()
        connection.close()

    assert server.authenticated_as("alice", fake_password)
    assert server.sources == ["6*7"]
    assert server.errors == []


def test_auth_rejection_and_errors_never_expose_password() -> None:
    fake_password = "fixture-password"
    with ScriptedQServer([], handshake_version=0) as server:
        connection = QConnection(
            server.host,
            server.port,
            username="alice",
            password=fake_password,
        )
        with pytest.raises(QIpcError) as captured:
            connection.connect()

    assert fake_password not in str(captured.value)
    assert fake_password not in repr(captured.value)
    assert fake_password not in repr(connection)
    assert captured.value.__context__ is None
    assert captured.value.__cause__ is None


def test_handshake_rejects_versions_newer_than_the_advertised_capability() -> None:
    with ScriptedQServer([], handshake_version=255) as server:
        with pytest.raises(QIpcError, match="authentication"):
            QConnection(server.host, server.port).connect()


def test_verbose_traceback_locals_do_not_capture_plaintext_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "trace-local-secret-9e7f"

    class FailingSocket:
        def settimeout(self, _timeout: float | None) -> None:
            return None

        def sendall(self, _data: bytes | bytearray) -> None:
            raise OSError(f"failed while sending {secret}")

        def shutdown(self, _how: int) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "kx_notebook.ipc.socket.create_connection",
        lambda *_args, **_kwargs: FailingSocket(),
    )
    connection = QConnection("localhost", 5000, username="alice", password=secret)
    try:
        connection.connect()
    except QIpcError as error:
        captured = package_traceback_locals(error)
        context = error.__context__
        cause = error.__cause__
    else:  # pragma: no cover - the fixture always raises
        pytest.fail("expected the fake socket to fail")

    assert secret not in captured
    assert context is None
    assert cause is None


def test_q_error_text_is_redacted_when_server_echoes_credentials() -> None:
    fake_password = "fixture-echoed-password"
    response = q_message(q_error(f"access denied for alice:{fake_password}"))
    with ScriptedQServer([Exchange(response)]) as server:
        with QConnection(
            server.host,
            server.port,
            username="alice",
            password=fake_password,
        ) as connection:
            with pytest.raises(QError) as captured:
                connection.query(f'protected["{fake_password}"]')

    assert fake_password not in str(captured.value)
    assert fake_password not in repr(captured.value)
    assert fake_password not in package_traceback_locals(captured.value)
    assert captured.value.__context__ is None
    assert captured.value.__cause__ is None


def test_malformed_response_traceback_locals_do_not_capture_password() -> None:
    fake_password = "malformed-response-secret-7bd2"
    response = q_message(q_error(f"denied {fake_password}") + b"trailing")
    with ScriptedQServer([Exchange(response)]) as server:
        with QConnection(
            server.host,
            server.port,
            password=fake_password,
        ) as connection:
            with pytest.raises(QIpcError) as captured:
                connection.query(f'bad["{fake_password}"]')

    assert fake_password not in str(captured.value)
    assert fake_password not in package_traceback_locals(captured.value)
    assert captured.value.__context__ is None
    assert captured.value.__cause__ is None


def test_query_timeout_closes_connection_and_redacts_credentials() -> None:
    fake_password = "fixture-password"
    with ScriptedQServer([Exchange(q_message(q_int(42)), delay=0.25)]) as server:
        connection = QConnection(
            server.host,
            server.port,
            username="alice",
            password=fake_password,
            query_timeout=0.03,
        )
        connection.connect()
        with pytest.raises(QTimeoutError) as captured:
            connection.query('{system"sleep 1";42}[]')
        connection.close()

    assert fake_password not in str(captured.value)
    assert fake_password not in repr(captured.value)
    assert server.sources == ['{system"sleep 1";42}[]']


def test_close_cancels_an_in_flight_sync_query() -> None:
    with ScriptedQServer([Exchange(None, delay=1.0)]) as server:
        connection = QConnection(
            server.host,
            server.port,
            query_timeout=2.0,
        )
        connection.connect()
        outcome: list[BaseException | object] = []

        def query() -> None:
            try:
                outcome.append(connection.query("wait[]"))
            except BaseException as error:
                outcome.append(error)

        thread = threading.Thread(target=query)
        thread.start()
        deadline = time.monotonic() + 1
        while not server.sources and time.monotonic() < deadline:
            time.sleep(0.005)
        started = time.monotonic()
        connection.cancel()
        cancel_elapsed = time.monotonic() - started
        thread.join(timeout=1)

    assert cancel_elapsed < 0.5
    assert not thread.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], QCancelledError)


def test_close_cancels_an_in_flight_handshake() -> None:
    with ScriptedQServer([], handshake_delay=1.0) as server:
        connection = QConnection(server.host, server.port, connect_timeout=2.0)
        outcome: list[BaseException | object] = []

        def connect() -> None:
            try:
                outcome.append(connection.connect())
            except BaseException as error:
                outcome.append(error)

        thread = threading.Thread(target=connect)
        thread.start()
        time.sleep(0.05)
        started = time.monotonic()
        connection.cancel()
        elapsed = time.monotonic() - started
        thread.join(timeout=1)

    assert elapsed < 0.5
    assert not thread.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], QCancelledError)


def test_decoder_checks_deadline_during_large_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = 0

    def monotonic() -> float:
        nonlocal ticks
        ticks += 1
        return 0.0 if ticks < 6 else 2.0

    monkeypatch.setattr("kx_notebook.ipc.time.monotonic", monotonic)
    message = q_message(q_int_vector(list(range(20_000))))

    with pytest.raises(socket.timeout, match="deadline"):
        deserialize_message(message, deadline=1.0)


def test_secret_redaction_handles_char_vectors_tables_and_key_collisions() -> None:
    secret = "fixture-secret"
    vector = QCharVector(secret.encode())
    table = QTable(["value"], [vector], len(vector))

    redacted_vector = redact_q_value(vector, secret)
    redacted_table = redact_q_value(table, secret)
    collision = redact_q_value({secret: 1, "█": 2}, secret)

    assert isinstance(redacted_vector, QCharVector)
    assert secret.encode() not in redacted_vector.raw
    assert isinstance(redacted_table, QTable)
    assert secret not in "".join(str(row["value"]) for row in redacted_table.rows)
    assert isinstance(collision, dict)
    assert len(collision) == 2
    assert sorted(collision.values()) == [1, 2]

    boundary_secret = "[redacted]x"
    connection = QConnection("localhost", 5000, password=boundary_secret)
    assert boundary_secret not in connection.redact_text(boundary_secret + "x")

    binary_secret = "*a"
    for original in (QCharVector(b"*aa"), QSymbol(b"*aa"), b"*aa"):
        redacted = redact_q_value(original, binary_secret)
        raw = redacted.raw if isinstance(redacted, (QCharVector, QSymbol)) else redacted
        assert binary_secret.encode() not in raw


def test_password_is_redacted_when_it_overlaps_endpoint_identity() -> None:
    secret = "fixture-secret"
    connection = QConnection(secret, 5000, username=secret, password=secret)

    assert secret not in repr(connection)


def test_non_utf8_credentials_are_rejected_without_echoing_them() -> None:
    secret = "pw-KEEP-SECRET-\ud800-tail"

    with pytest.raises(ValueError) as captured:
        QConnection("localhost", 5000, password=secret)

    assert "KEEP-SECRET" not in str(captured.value)
    assert "KEEP-SECRET" not in repr(captured.value)


def test_receive_limit_rejects_oversized_response_before_materializing_it() -> None:
    response = q_message(q_string("x" * 2_000))
    with ScriptedQServer([Exchange(response, chunk_sizes=(8,))]) as server:
        with QConnection(
            server.host,
            server.port,
            max_receive_bytes=1_024,
        ) as connection:
            with pytest.raises(QIpcError, match="receive|message|limit|large"):
                connection.query("big[]")
