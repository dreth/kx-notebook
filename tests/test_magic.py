from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from IPython.core.error import UsageError
from IPython.core.interactiveshell import InteractiveShell
from IPython.utils.capture import capture_output

from kx_notebook import (
    MIME_TYPE,
    clear_evaluator,
    configure_evaluator,
    display_result,
)
from kx_notebook.config import Config, Profile, save_config
from kx_notebook.contract import canonical_payload_bytes
from kx_notebook.defaults import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_QUERY_TIMEOUT_SECONDS,
)
from kx_notebook.evaluators import DirectQEvaluator, EvaluatorError
from kx_notebook.ipc import QConnection
from kx_notebook.magic import (
    KxQMagics,
    load_ipython_extension,
    unload_ipython_extension,
)

from .qipc_fixtures import (
    Exchange,
    ScriptedQServer,
    q_direct_result,
    q_float_vector,
    q_message,
    q_symbol_vector,
    q_table,
)


@pytest.fixture(autouse=True)
def _clear_global_evaluator() -> None:
    clear_evaluator()
    yield
    clear_evaluator()


def test_cell_magic_calls_exact_callback_and_publishes_raw_bundle() -> None:
    calls: list[str] = []

    def evaluator(source: str) -> list[dict[str, object]]:
        calls.append(source)
        return [{"sym": "AAPL", "price": 224.1}]

    configure_evaluator(evaluator, label="test callback")
    magic = KxQMagics(shell=None)
    with mock.patch("IPython.display.display") as display:
        assert magic.q("", "select from trade") is None

    assert calls == ["select from trade"]
    display.assert_called_once()
    bundle = display.call_args.args[0]
    assert display.call_args.kwargs["raw"] is True
    assert bundle[MIME_TYPE]["provenance"]["label"] == "test callback"
    assert "elapsedMs" in bundle[MIME_TYPE]["provenance"]
    assert "qSource" not in bundle[MIME_TYPE]["provenance"]


def test_display_result_emits_custom_html_and_text_mime_through_ipython() -> None:
    InteractiveShell.instance()
    with capture_output() as captured:
        display_result([{"x": 1}])

    assert len(captured.outputs) == 1
    assert set(captured.outputs[0].data) == {MIME_TYPE, "text/html", "text/plain"}
    assert captured.outputs[0].data[MIME_TYPE]["version"] == 1


@pytest.mark.parametrize(
    "secret",
    [
        "a\tb",
        "a</td><td>b",
        "x (string), y",
        'a"},{"kind":"string","value":"b',
    ],
)
def test_pre_display_gate_omits_credentials_reconstructed_by_serialization(
    secret: str,
) -> None:
    connection = QConnection("localhost", 5000, password=secret)
    with mock.patch("IPython.display.display") as display:
        output = display_result(
            [{"x": "a", "y": "b"}],
            redact_text=connection.redact_text,
        )

    display.assert_called_once()
    bundle = display.call_args.args[0]
    serialized = (
        canonical_payload_bytes(bundle[MIME_TYPE]).decode("utf-8")
        + bundle["text/html"]
        + bundle["text/plain"]
    )
    assert bundle[MIME_TYPE]["kind"] == "qText"
    assert "result omitted" in bundle["text/plain"]
    assert secret not in serialized
    assert output.bundle == bundle


def test_pre_display_gate_suppresses_output_if_fixed_contract_text_matches_secret() -> None:
    connection = QConnection("localhost", 5000, password="version")
    with mock.patch("IPython.display.display") as display:
        output = display_result(
            [{"x": 1}],
            redact_text=connection.redact_text,
        )

    display.assert_not_called()
    assert output.bundle == {}
    assert output.body_bytes == 0


def test_source_persistence_is_explicit_opt_in() -> None:
    configure_evaluator(
        lambda _source: [{"ok": True}],
        include_q_source=True,
    )
    with mock.patch("IPython.display.display") as display:
        KxQMagics(shell=None).q("", "show `runtime_secret")

    payload = display.call_args.args[0][MIME_TYPE]
    assert payload["provenance"]["qSource"] == "show `runtime_secret"


def test_cell_options_override_limits_and_label() -> None:
    configure_evaluator(
        lambda _source: [{"id": index} for index in range(5)],
        label="configured",
        row_limit=10,
        byte_limit=100_000,
    )
    with mock.patch("IPython.display.display") as display:
        KxQMagics(shell=None).q(
            '--max-rows 2 --max-bytes 20000 --label "cell preview"',
            "select from t",
        )

    payload = display.call_args.args[0][MIME_TYPE]
    assert payload["result"]["rowLimit"] == 2
    assert payload["result"]["byteLimit"] == 20_000
    assert payload["result"]["previewRowCount"] == 2
    assert payload["provenance"]["label"] == "cell preview"


@pytest.mark.parametrize(
    "line",
    [
        "--max-rows 0",
        "--max-rows 10001",
        "--max-bytes nope",
        "--max-bytes 10000001",
        "--max-rows 2 --max-rows 3",
        "--unknown 3",
        "--label",
    ],
)
def test_invalid_cell_options_do_not_execute_q(line: str) -> None:
    evaluator = mock.Mock(return_value=[{"x": 1}])
    configure_evaluator(evaluator)

    with pytest.raises(UsageError):
        KxQMagics(shell=None).q(line, "1+1")

    evaluator.assert_not_called()


def test_magic_rejects_missing_evaluator_and_awaitable_callback() -> None:
    magic = KxQMagics(shell=None)
    with pytest.raises(UsageError, match="No q evaluator"):
        magic.q("", "1+1")

    async def async_evaluator(_source: str) -> list[dict[str, bool]]:
        return [{"ok": True}]

    configure_evaluator(async_evaluator)
    with pytest.raises(
        (UsageError, EvaluatorError),
        match="awaitable|synchronous|async",
    ):
        magic.q("", "1+1")


def test_extension_hooks_register_and_unregister_magics() -> None:
    shell = mock.Mock()
    shell.magics_manager.magics = {
        "cell": {"q": object()},
        "line": {"kx": object()},
    }

    load_ipython_extension(shell)
    shell.register_magics.assert_called_once_with(KxQMagics)
    unload_ipython_extension(shell)

    assert "q" not in shell.magics_manager.magics["cell"]
    assert "kx" not in shell.magics_manager.magics["line"]


def test_real_ipython_run_cell_uses_normal_cell_lifecycle() -> None:
    calls: list[str] = []
    shell = InteractiveShell.instance()
    load_ipython_extension(shell)
    configure_evaluator(
        lambda source: calls.append(source) or [{"x": 42}],
    )

    with capture_output() as captured:
        execution = shell.run_cell("%%q\n6*7")

    assert execution.error_before_exec is None
    assert execution.error_in_exec is None
    assert [source.rstrip("\n") for source in calls] == ["6*7"]
    assert len(captured.outputs) == 1
    assert captured.outputs[0].data[MIME_TYPE]["data"]["rows"][0][0]["value"] == 42
    unload_ipython_extension(shell)


def test_kx_help_status_profiles_and_disconnect_are_safe_without_connection() -> None:
    magic = KxQMagics(shell=None)

    with capture_output() as captured:
        magic.kx("help")
        magic.kx("status")
        magic.kx("profiles")
        magic.kx("disconnect")

    text = captured.stdout.lower()
    assert "connect" in text
    assert "status" in text
    assert "disconnected" in text or "not connected" in text


def test_kx_connect_uses_shared_timeout_defaults_when_omitted() -> None:
    with mock.patch("kx_notebook.magic.DirectQEvaluator") as evaluator_type:
        evaluator_type.return_value.endpoint = "localhost:5000"

        KxQMagics(shell=None).kx("connect localhost:5000")

    evaluator_type.assert_called_once_with(
        "localhost",
        5000,
        username="",
        password=None,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS,
        query_timeout=DEFAULT_QUERY_TIMEOUT_SECONDS,
    )
    evaluator_type.return_value.connect.assert_called_once_with()


@pytest.mark.integration
def test_real_ipython_kx_connect_q_and_disconnect_lifecycle() -> None:
    response = q_message(
        q_direct_result(
            q_table(
                {
                    "sym": q_symbol_vector(["AAPL"]),
                    "price": q_float_vector([224.1]),
                }
            ),
            kind="table",
            row_count=1,
        )
    )
    shell = InteractiveShell.instance()
    load_ipython_extension(shell)
    try:
        with ScriptedQServer([Exchange(response)]) as server:
            with capture_output() as captured:
                shell.run_line_magic("kx", f"connect {server.host}:{server.port}")
                execution = shell.run_cell("%%q\nselect from trade")
                shell.run_line_magic("kx", "status")
                shell.run_line_magic("kx", "disconnect")

        assert execution.error_before_exec is None
        assert execution.error_in_exec is None
        assert len(server.sources) == 1
        assert "select from trade" in server.sources[0]
        assert len(captured.outputs) == 1
        payload = captured.outputs[0].data[MIME_TYPE]
        assert payload["kind"] == "table"
        assert payload["data"]["rows"][0][0]["value"] == "AAPL"
        assert str(server.port) in captured.stdout
    finally:
        unload_ipython_extension(shell)


@pytest.mark.integration
def test_direct_magic_wires_the_final_serialized_credential_gate() -> None:
    secret = "a\tb"
    response = q_message(
        q_direct_result(
            q_table(
                {
                    "x": q_symbol_vector(["a"]),
                    "y": q_symbol_vector(["b"]),
                }
            ),
            kind="table",
            row_count=1,
        )
    )
    with ScriptedQServer([Exchange(response)]) as server:
        configure_evaluator(DirectQEvaluator(server.host, server.port, password=secret))
        with mock.patch("IPython.display.display") as display:
            KxQMagics(shell=None).q("", "table[]")

    bundle = display.call_args.args[0]
    assert bundle[MIME_TYPE]["kind"] == "qText"
    assert secret not in (
        canonical_payload_bytes(bundle[MIME_TYPE]).decode("utf-8")
        + bundle["text/html"]
        + bundle["text/plain"]
    )


@pytest.mark.integration
def test_kx_profiles_and_use_resolve_runtime_password_without_printing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_password = "fixture-profile-password"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("KX_NOTEBOOK_TEST_PASSWORD", fake_password)
    response = q_message(
        q_direct_result(
            q_table(
                {
                    "sym": q_symbol_vector(["AAPL"]),
                    "price": q_float_vector([224.1]),
                }
            ),
            kind="table",
            row_count=1,
        )
    )
    with ScriptedQServer([Exchange(response)]) as server:
        save_config(
            Config(
                profiles={
                    "local": Profile(
                        name="local",
                        host=server.host,
                        port=server.port,
                        username="alice",
                        password_env="KX_NOTEBOOK_TEST_PASSWORD",
                    )
                },
                default_profile="local",
            )
        )
        magic = KxQMagics(shell=None)
        with capture_output() as captured:
            magic.kx("profiles")
            magic.kx("use local")
            with mock.patch("IPython.display.display") as display:
                magic.q("", "select from trade")
            magic.kx("status")
            magic.kx("disconnect")

    assert server.authenticated_as("alice", fake_password)
    assert len(server.sources) == 1
    assert "select from trade" in server.sources[0]
    assert "local" in captured.stdout
    assert fake_password not in captured.stdout
    assert fake_password not in captured.stderr
    assert display.call_args.args[0][MIME_TYPE]["kind"] == "table"
