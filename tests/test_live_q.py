from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from kx_notebook import MIME_TYPE, build_mime_bundle
from kx_notebook.contract import QText
from kx_notebook.evaluators import DirectQEvaluator, EvaluationContext
from kx_notebook.ipc import (
    DEFAULT_MAX_ITEMS,
    DEFAULT_MAX_RECEIVE_BYTES,
    QCharVector,
    QConnection,
    QError,
    QTable,
    QTimeoutError,
)


def _q_executable() -> Path | None:
    configured = os.environ.get("KX_Q_PATH")
    candidates = [
        Path(configured) if configured else None,
        Path.home() / ".kx" / "bin" / "q",
        Path(shutil.which("q")) if shutil.which("q") else None,
    ]
    return next(
        (
            candidate
            for candidate in candidates
            if candidate is not None and candidate.is_file() and os.access(candidate, os.X_OK)
        ),
        None,
    )


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture
def live_q(tmp_path: Path) -> Iterator[tuple[str, int]]:
    executable = _q_executable()
    if executable is None:
        pytest.skip("no local q executable found")
    port = _unused_port()
    process = subprocess.Popen(
        [str(executable), "-q", "-p", str(port)],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            pytest.fail(
                f"q exited before listening (code {process.returncode}): "
                f"{(stdout + stderr)[-1000:]}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                break
        except OSError:
            time.sleep(0.02)
    else:
        process.terminate()
        process.wait(timeout=5)
        pytest.fail("q did not listen within 10 seconds")

    try:
        yield "127.0.0.1", port
    finally:
        if process.poll() is None and process.stdin is not None:
            try:
                process.stdin.write("\\\\\n")
                process.stdin.flush()
                process.wait(timeout=3)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                process.terminate()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


@pytest.mark.live_q
@pytest.mark.integration
def test_live_q_scalar_list_table_error_timeout_and_reconnect(
    live_q: tuple[str, int],
) -> None:
    host, port = live_q
    with QConnection(host, port, query_timeout=2) as connection:
        assert connection.query("1+1") == 2
        assert connection.query("1 2 3") == [1, 2, 3]
        table = connection.query("([] sym:`AAPL`MSFT;price:10.5 11.25;size:100 200)")
        assert isinstance(table, QTable)
        assert list(table.columns) == ["sym", "price", "size"]
        assert table.row_count == 2
        assert list(table.rows)[0] == {
            "sym": "AAPL",
            "price": 10.5,
            "size": 100,
        }
        with pytest.raises(QError):
            connection.query("`a+1")

    timed = QConnection(host, port, query_timeout=0.03)
    timed.connect()
    with pytest.raises(QTimeoutError):
        timed.query('{system"sleep 1";42}[]')
    timed.close()

    # Timeout closes only the client. q remains reachable after its work completes.
    time.sleep(1.1)
    with QConnection(host, port, query_timeout=2) as reconnected:
        assert reconnected.query("6*7") == 42


@pytest.mark.live_q
@pytest.mark.integration
def test_live_q_direct_evaluator_bounds_500k_by_40_table_on_the_wire(
    live_q: tuple[str, int],
) -> None:
    host, port = live_q
    rows = 500_000
    columns = 40
    assert rows * columns > DEFAULT_MAX_ITEMS
    assert rows * columns * 4 > DEFAULT_MAX_RECEIVE_BYTES

    evaluator = DirectQEvaluator(
        host,
        port,
        max_receive_bytes=4_096,
        query_timeout=20,
    )
    try:
        result = evaluator.evaluate(
            'wireRuns:0\nwireRuns+:1\nflip (`$("c",/:string til 40))!40#enlist til 500000\n',
            EvaluationContext(row_limit=3, byte_limit=20_000),
        )
        state = evaluator.evaluate("wireRuns")
    finally:
        evaluator.close()

    assert result.row_count == rows
    assert result.columns == [f"c{index}" for index in range(columns)]
    assert len(result.value) == 3
    assert list(result.value)[0] == {f"c{index}": 0 for index in range(columns)}
    assert list(result.value)[2] == {f"c{index}": 2 for index in range(columns)}
    assert isinstance(state.value, QText)
    assert state.value.text == "1"

    output = build_mime_bundle(
        result.value,
        columns=result.columns,
        row_count=result.row_count,
        row_limit=3,
        byte_limit=20_000,
    )
    payload = output.bundle[MIME_TYPE]
    assert payload["result"]["rowCount"] == rows
    assert payload["result"]["previewRowCount"] == 3
    assert payload["result"]["truncated"] is True
    assert len(payload["data"]["rows"]) == 3


@pytest.mark.live_q
@pytest.mark.integration
def test_live_q_direct_envelope_preserves_namespace_state_and_errors(
    live_q: tuple[str, int],
) -> None:
    host, port = live_q
    evaluator = DirectQEvaluator(host, port, namespace=".analytics")
    try:
        result = evaluator.evaluate(
            "counter:0\ncounter+:1\n([]id:til 5)",
            EvaluationContext(row_limit=2, byte_limit=20_000),
        )
        state = evaluator.evaluate("counter")
        with pytest.raises(QError, match="boom"):
            evaluator.evaluate("counter+:1\n'`boom")
        state_after_error = evaluator.evaluate("counter")
    finally:
        evaluator.close()

    assert result.row_count == 5
    assert list(result.value) == [{"id": 0}, {"id": 1}]
    assert isinstance(state.value, QText)
    assert state.value.text == "1"
    assert isinstance(state_after_error.value, QText)
    assert state_after_error.value.text == "2"
    with QConnection(host, port, query_timeout=2) as connection:
        namespace = connection.query('string system"d"')
    assert isinstance(namespace, QCharVector)
    assert namespace.text() == "."
