from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from kx_notebook.contract import (
    JS_SAFE_INTEGER,
    MIME_TYPE,
    EvaluationResult,
    QText,
    build_mime_bundle,
)
from kx_notebook.evaluators import (
    CallbackEvaluator,
    DirectQEvaluator,
    EvaluationContext,
    EvaluatorError,
    PyKXEvaluator,
)
from kx_notebook.ipc import QError, QIpcError, QTimeoutError

from .qipc_fixtures import (
    DIRECT_Q_ENVELOPE_MARKER,
    Exchange,
    ScriptedQServer,
    q_bool,
    q_dictionary,
    q_direct_result,
    q_error,
    q_float_vector,
    q_general_list,
    q_int,
    q_int_vector,
    q_keyed_table,
    q_long,
    q_message,
    q_string,
    q_symbol,
    q_symbol_vector,
    q_table,
)


def test_callback_evaluator_calls_the_exact_source_once() -> None:
    calls: list[str] = []
    expected = EvaluationResult([{"answer": 42}], label="fixture")

    def callback(source: str) -> EvaluationResult:
        calls.append(source)
        return expected

    evaluator = CallbackEvaluator(callback)
    actual = evaluator.evaluate(
        "6*7",
        EvaluationContext(row_limit=3, byte_limit=20_000, timeout=0.5),
    )

    assert actual is expected
    assert calls == ["6*7"]


def test_callback_evaluator_rejects_noncallables_and_awaitables() -> None:
    with pytest.raises(TypeError, match="callable"):
        CallbackEvaluator(None)  # type: ignore[arg-type]

    async def callback(_source: str) -> list[dict[str, bool]]:
        return [{"ok": True}]

    with pytest.raises((TypeError, RuntimeError), match="awaitable|synchronous|async"):
        CallbackEvaluator(callback).evaluate("1b")


def test_evaluation_context_validates_limits_before_evaluation() -> None:
    callback = CallbackEvaluator(lambda _source: [{"ok": True}])
    for kwargs in (
        {"row_limit": 0},
        {"row_limit": 10_001},
        {"byte_limit": 16_383},
        {"byte_limit": 10_000_001},
        {"timeout": -1},
        {"timeout": True},
        {"timeout": "5"},
    ):
        with pytest.raises((TypeError, ValueError)):
            callback.evaluate("1b", EvaluationContext(**kwargs))  # type: ignore[arg-type]


def test_direct_evaluator_bounds_a_table_preview_and_retains_total_count() -> None:
    preview = q_table(
        {
            "sym": q_symbol_vector(["AAPL", "MSFT"]),
            "price": q_float_vector([224.1, 518.0]),
        }
    )
    response = q_message(q_direct_result(preview, kind="table", row_count=60_000))
    with ScriptedQServer([Exchange(response)]) as server:
        evaluator = DirectQEvaluator(server.host, server.port)
        result = evaluator.evaluate(
            "select from trade",
            EvaluationContext(row_limit=2, byte_limit=100_000),
        )
        evaluator.close()

    assert isinstance(result, EvaluationResult)
    assert list(result.columns or ()) == ["sym", "price"]
    assert result.row_count == 60_000
    assert len(response) < 1_024
    output = build_mime_bundle(
        result.value,
        columns=result.columns,
        row_count=result.row_count,
        row_limit=2,
        byte_limit=100_000,
    )
    payload = output.bundle[MIME_TYPE]
    assert payload["result"]["rowCount"] == 60_000
    assert payload["result"]["previewRowCount"] == 2
    assert payload["result"]["truncated"] is True
    assert "select from trade" in server.sources[0]
    assert DIRECT_Q_ENVELOPE_MARKER in server.sources[0]
    assert "60000" not in server.sources[0]


def test_direct_evaluator_bounds_keyed_table_transport_and_retains_total_count() -> None:
    preview = q_keyed_table(
        {"id": q_int_vector([0, 1, 2])},
        {"price": q_float_vector([10.0, 11.0, 12.0])},
    )
    response = q_message(q_direct_result(preview, kind="keyedTable", row_count=500_000))
    with ScriptedQServer([Exchange(response)]) as server:
        evaluator = DirectQEvaluator(server.host, server.port)
        result = evaluator.evaluate(
            "([] id:til 500000)!([] price:500000#10f)",
            EvaluationContext(row_limit=3, byte_limit=100_000),
        )
        evaluator.close()

    assert result.columns == ["id", "price"]
    assert result.row_count == 500_000
    assert list(result.value) == [
        {"id": 0, "price": 10.0},
        {"id": 1, "price": 11.0},
        {"id": 2, "price": 12.0},
    ]
    assert len(response) < 1_024


@pytest.mark.parametrize(
    ("kind", "column_count", "message"),
    [
        ("tableColumns", 257, "columns exceed"),
        ("tableBytes", 40, "wire limit"),
    ],
)
def test_direct_evaluator_handles_server_side_table_omission_envelopes(
    kind: str,
    column_count: int,
    message: str,
) -> None:
    response = q_direct_result(q_long(column_count), kind=kind, row_count=500_000)
    with ScriptedQServer([Exchange(q_message(response))]) as server:
        evaluator = DirectQEvaluator(server.host, server.port)
        result = evaluator.evaluate("largeTable[]")
        evaluator.close()

    assert isinstance(result.value, QText)
    assert result.value.truncated is True
    assert f"500000x{column_count}" in result.value.text
    assert message in result.value.text
    assert result.columns is None
    assert result.row_count is None


def test_direct_evaluator_accepts_a_strict_wire_capped_table_preview() -> None:
    preview = q_table({"x": q_int_vector([0])})
    response = q_direct_result(preview, kind="tableSafe", row_count=500_000)
    with ScriptedQServer([Exchange(q_message(response))]) as server:
        evaluator = DirectQEvaluator(server.host, server.port)
        result = evaluator.evaluate(
            "largeTable[]",
            EvaluationContext(row_limit=10_000, byte_limit=100_000),
        )
        evaluator.close()

    assert result.row_count == 500_000
    assert result.columns == ["x"]
    assert list(result.value) == [{"x": 0}]


def test_direct_evaluator_envelope_cannot_collide_with_user_dictionary_data() -> None:
    collision = q_dictionary(
        q_symbol_vector(["marker", "kind", "rowCount", "value"]),
        q_general_list(
            [
                q_string(DIRECT_Q_ENVELOPE_MARKER),
                q_symbol("table"),
                q_int(999),
                q_table({"x": q_int_vector([1])}),
            ]
        ),
    )
    response = q_message(q_direct_result(collision, kind="value"))
    with ScriptedQServer([Exchange(response)]) as server:
        evaluator = DirectQEvaluator(server.host, server.port)
        result = evaluator.evaluate(
            '`marker`kind`rowCount`value!( "kx-notebook/direct-q/v1";`table;999;([]x:enlist 1))'
        )
        evaluator.close()

    assert result.row_count is None
    assert result.columns is None
    assert isinstance(result.value, QText)
    assert DIRECT_Q_ENVELOPE_MARKER in result.value.text
    assert "marker" in result.value.text


@pytest.mark.parametrize(
    "payload",
    [
        q_int(42),
        q_general_list(
            [
                q_string(DIRECT_Q_ENVELOPE_MARKER),
                q_symbol("value"),
                q_long(-9_223_372_036_854_775_808),
            ]
        ),
        q_direct_result(q_int(42), kind="table", row_count=1),
        q_direct_result(q_table({"x": q_int_vector([1])}), kind="value"),
        q_direct_result(q_table({"x": q_int_vector([1])}), kind="keyedTable", row_count=1),
        q_direct_result(q_table({"x": q_int_vector([1])}), kind="unknown", row_count=1),
        q_direct_result(q_table({"x": q_int_vector([1, 2])}), kind="table", row_count=1),
        q_direct_result(q_table({"x": q_int_vector([1])}), kind="table", row_count=2),
        q_general_list(
            [
                q_string("wrong-marker"),
                q_symbol("value"),
                q_long(-9_223_372_036_854_775_808),
                q_int(42),
            ]
        ),
        q_general_list(
            [
                q_symbol(DIRECT_Q_ENVELOPE_MARKER),
                q_symbol("value"),
                q_long(-9_223_372_036_854_775_808),
                q_int(42),
            ]
        ),
        q_general_list(
            [
                q_string(DIRECT_Q_ENVELOPE_MARKER),
                q_string("value"),
                q_long(-9_223_372_036_854_775_808),
                q_int(42),
            ]
        ),
        q_general_list(
            [
                q_string(DIRECT_Q_ENVELOPE_MARKER),
                q_symbol("value"),
                q_int(0),
                q_int(42),
            ]
        ),
        q_general_list(
            [
                q_string(DIRECT_Q_ENVELOPE_MARKER),
                q_symbol("table"),
                q_bool(True),
                q_table({"x": q_int_vector([1])}),
            ]
        ),
        q_direct_result(q_table({"x": q_int_vector([1])}), kind="table", row_count=-1),
        q_direct_result(
            q_table({"x": q_int_vector([1])}),
            kind="table",
            row_count=JS_SAFE_INTEGER + 1,
        ),
        q_direct_result(
            q_table({"x": q_int_vector(list(range(21)))}),
            kind="table",
            row_count=21,
        ),
    ],
)
def test_direct_evaluator_rejects_malformed_internal_envelopes(payload: bytes) -> None:
    with ScriptedQServer([Exchange(q_message(payload))]) as server:
        evaluator = DirectQEvaluator(server.host, server.port)
        with pytest.raises((EvaluatorError, QIpcError), match="envelope"):
            evaluator.evaluate("42")
        evaluator.close()


def test_malformed_envelope_does_not_retain_password_in_traceback_locals() -> None:
    secret = "malformed-envelope-secret"
    payload = q_general_list(
        [
            q_string(DIRECT_Q_ENVELOPE_MARKER),
            q_symbol("table"),
            q_long(1),
            q_string(secret),
        ]
    )
    with ScriptedQServer([Exchange(q_message(payload))]) as server:
        evaluator = DirectQEvaluator(server.host, server.port, password=secret)
        with pytest.raises(EvaluatorError, match="envelope") as captured:
            evaluator.evaluate("42")
        evaluator.close()

    traceback = captured.value.__traceback__
    while traceback is not None:
        filename = traceback.tb_frame.f_code.co_filename
        if "/src/kx_notebook/" in filename:
            assert secret not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
    assert captured.value.__context__ is None


def test_direct_evaluator_normalizes_scalar_for_portable_display() -> None:
    response = q_direct_result(q_int(42), kind="value")
    with ScriptedQServer([Exchange(q_message(response))]) as server:
        evaluator = DirectQEvaluator(server.host, server.port)
        result = evaluator.evaluate("6*7")
        evaluator.close()

    assert isinstance(result, EvaluationResult)
    assert isinstance(result.value, QText)
    assert "42" in result.value.text


def test_direct_evaluator_preserves_opaque_non_table_fallback_inside_envelope() -> None:
    response = q_direct_result(bytes((20,)), kind="value")
    with ScriptedQServer([Exchange(q_message(response))]) as server:
        evaluator = DirectQEvaluator(server.host, server.port)
        result = evaluator.evaluate("opaque[]")
        evaluator.close()

    assert isinstance(result.value, QText)
    assert result.value.truncated is True
    assert "unsupported q IPC type 20" in result.value.text


def test_direct_evaluator_redacts_a_password_spanning_a_char_table_column() -> None:
    secret = "table-secret"
    table = q_table({"c": q_string(secret)})
    response = q_direct_result(table, kind="table", row_count=len(secret))
    with ScriptedQServer([Exchange(q_message(response))]) as server:
        evaluator = DirectQEvaluator(server.host, server.port, password=secret)
        result = evaluator.evaluate('([]c:"table-secret")')
        evaluator.close()

    output = build_mime_bundle(
        result.value,
        columns=result.columns,
        row_count=result.row_count,
        byte_limit=100_000,
    )
    payload_text = str(output.bundle)
    reconstructed = "".join(
        str(row[0]["value"]) for row in output.bundle[MIME_TYPE]["data"]["rows"]
    )

    assert secret not in payload_text
    assert reconstructed != secret


def test_direct_evaluator_preserves_columns_that_collide_after_redaction() -> None:
    secret = "collision"
    table = q_table(
        {
            secret: q_string("a"),
            "█": q_string("b"),
        }
    )
    response = q_direct_result(table, kind="table", row_count=1)
    with ScriptedQServer([Exchange(q_message(response))]) as server:
        evaluator = DirectQEvaluator(server.host, server.port, password=secret)
        result = evaluator.evaluate("table[]")
        evaluator.close()

    assert result.columns == ["█", "██"]
    assert list(result.value) == [{"█": "a", "██": "b"}]
    assert secret not in repr(result)

    output = build_mime_bundle(
        result.value,
        columns=result.columns,
        row_count=result.row_count,
    )
    assert secret not in str(output.bundle)


def test_direct_evaluator_downgrades_overlong_column_names_safely() -> None:
    long_name = "x" * 257
    table = q_table({long_name: q_string("a")})
    response = q_direct_result(table, kind="table", row_count=1)
    with ScriptedQServer([Exchange(q_message(response))]) as server:
        evaluator = DirectQEvaluator(server.host, server.port)
        result = evaluator.evaluate("table[]")
        evaluator.close()

    assert isinstance(result.value, QText)
    assert result.value.truncated is True
    assert result.value.truncation_reasons == ("columnLimit",)


def test_direct_evaluator_propagates_q_error_and_timeout() -> None:
    with ScriptedQServer(
        [
            Exchange(q_message(q_error("type"))),
            Exchange(q_message(q_int(42)), delay=0.2),
        ]
    ) as server:
        evaluator = DirectQEvaluator(
            server.host,
            server.port,
            query_timeout=0.03,
        )
        with pytest.raises(QError, match="type"):
            evaluator.evaluate("`a+1")
        with pytest.raises(QTimeoutError):
            evaluator.evaluate("slow[]")
        evaluator.close()


def test_direct_evaluator_repr_and_errors_never_include_password() -> None:
    fake_password = "fixture-direct-password"
    with ScriptedQServer([], handshake_version=0) as server:
        evaluator = DirectQEvaluator(
            server.host,
            server.port,
            username="alice",
            password=fake_password,
        )
        assert fake_password not in repr(evaluator)
        with pytest.raises(Exception) as captured:
            evaluator.evaluate("1+1")

    assert fake_password not in str(captured.value)
    assert fake_password not in repr(captured.value)


def test_pykx_is_not_imported_by_the_base_package() -> None:
    command = [
        sys.executable,
        "-c",
        ("import sys; import kx_notebook; raise SystemExit(1 if 'pykx' in sys.modules else 0)"),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr


def test_pykx_adapter_slices_before_converting_to_python() -> None:
    trace: list[tuple[Any, ...]] = []

    class FakeKxTable:
        def __init__(self, rows: list[dict[str, int]]) -> None:
            self.rows = rows

        def __len__(self) -> int:
            return len(self.rows)

        def __getitem__(self, item: slice) -> FakeKxTable:
            trace.append(("slice", item.start, item.stop))
            return FakeKxTable(self.rows[item])

        def py(self) -> list[dict[str, int]]:
            trace.append(("py", len(self.rows)))
            return self.rows

    calls: list[str] = []

    def q(source: str) -> FakeKxTable:
        calls.append(source)
        return FakeKxTable([{"x": index} for index in range(20)])

    result = PyKXEvaluator(q=q).evaluate(
        "select from t",
        EvaluationContext(row_limit=3, byte_limit=100_000),
    )

    assert calls == ["select from t"]
    assert trace == [("slice", None, 3), ("py", 3)]
    assert isinstance(result, EvaluationResult)
    assert result.row_count == 20
    assert result.value == [{"x": 0}, {"x": 1}, {"x": 2}]


def test_pykx_vector_and_dictionary_use_bounded_qtext_previews() -> None:
    class FakeKxVector:
        def __init__(self, values: list[int]) -> None:
            self.values = values

        def __len__(self) -> int:
            return len(self.values)

        def __getitem__(self, item: slice) -> FakeKxVector:
            return FakeKxVector(self.values[item])

        def py(self) -> list[int]:
            return self.values

    class FakeKxDictionary:
        def __init__(self, keys: list[str], values: list[int]) -> None:
            self._keys = FakeKxVector(keys)  # type: ignore[arg-type]
            self._values = FakeKxVector(values)

        def __len__(self) -> int:
            return len(self._keys)

        def __getitem__(self, _item: slice) -> None:
            raise AssertionError("dictionary itself must not be sliced")

        def py(self) -> dict[str, int]:
            raise AssertionError("full dictionary must not be converted")

        def keys(self) -> FakeKxVector:
            return self._keys

        def values(self) -> FakeKxVector:
            return self._values

    context = EvaluationContext(row_limit=2, byte_limit=20_000)
    vector_result = PyKXEvaluator(q=lambda _source: FakeKxVector([1, 2, 3])).evaluate(
        "1 2 3", context
    )
    dictionary_result = PyKXEvaluator(
        q=lambda _source: FakeKxDictionary(["a", "b", "c"], [1, 2, 3])
    ).evaluate("`a`b`c!1 2 3", context)

    assert isinstance(vector_result.value, QText)
    assert vector_result.value.truncated is True
    assert vector_result.value.truncation_reasons == ("sourcePreview",)
    assert isinstance(dictionary_result.value, QText)
    assert dictionary_result.value.truncated is True
    assert dictionary_result.value.truncation_reasons == ("sourcePreview",)
    assert '"a"' in dictionary_result.value.text
    assert '"b"' in dictionary_result.value.text
    assert '"c"' not in dictionary_result.value.text


def test_pykx_keyed_table_uses_bounded_head_and_merges_columns() -> None:
    trace: list[tuple[str, int]] = []

    class Converted:
        def __init__(self, value: dict[str, list[Any]]) -> None:
            self.value = value

        def py(self) -> dict[str, list[Any]]:
            return self.value

    class FakeKxKeyedTable:
        def __init__(self, rows: int) -> None:
            self.rows = rows

        def __len__(self) -> int:
            return self.rows

        def __getitem__(self, _item: slice) -> None:
            raise AssertionError("keyed table must use head()")

        def py(self) -> None:
            raise AssertionError("full keyed table must not be converted")

        def head(self, count: int) -> FakeKxKeyedTable:
            trace.append(("head", count))
            return FakeKxKeyedTable(count)

        def keys(self) -> Converted:
            return Converted({"key": list(range(self.rows))})

        def values(self) -> Converted:
            return Converted({"value": [item * 10 for item in range(self.rows)]})

    result = PyKXEvaluator(q=lambda _source: FakeKxKeyedTable(10)).evaluate(
        "([] key:til 10)!([] value:10*til 10)",
        EvaluationContext(row_limit=3, byte_limit=20_000),
    )

    assert trace == [("head", 3)]
    assert result.value == {"key": [0, 1, 2], "value": [0, 10, 20]}
    assert result.row_count == 10


def test_missing_pykx_is_actionable_only_when_adapter_is_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "pykx", None)
    evaluator = PyKXEvaluator()

    with pytest.raises(RuntimeError, match="PyKX|pykx|install"):
        evaluator.evaluate("1+1")
