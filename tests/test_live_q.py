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
from kx_notebook.evaluators import DirectQEvaluator, EvaluationContext
from kx_notebook.ipc import QConnection, QError, QTable, QTimeoutError


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
def test_live_q_direct_evaluator_retains_total_count_for_bounded_preview(
    live_q: tuple[str, int],
) -> None:
    host, port = live_q
    evaluator = DirectQEvaluator(host, port)
    try:
        result = evaluator.evaluate(
            "([] id:til 100;sym:100#`AAPL)\n",
            EvaluationContext(row_limit=3, byte_limit=20_000),
        )
    finally:
        evaluator.close()

    output = build_mime_bundle(
        result.value,
        columns=result.columns,
        row_count=result.row_count,
        row_limit=3,
        byte_limit=20_000,
    )
    payload = output.bundle[MIME_TYPE]
    assert payload["result"]["rowCount"] == 100
    assert payload["result"]["previewRowCount"] == 3
    assert payload["result"]["truncated"] is True
