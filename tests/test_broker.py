from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from kx_notebook.contract import EvaluationResult, QText
from kx_notebook.defaults import DEFAULT_QUERY_TIMEOUT_SECONDS
from kx_notebook.evaluators import BrokerEvaluator, EvaluationContext


@dataclass(frozen=True)
class Request:
    path: str
    headers: dict[str, str]
    body: bytes


class BrokerServer:
    def __init__(
        self,
        response: Any,
        *,
        status: int = 200,
        delay: float = 0,
        raw: bool = False,
    ) -> None:
        self.response = response
        self.status = status
        self.delay = delay
        self.raw = raw
        self.requests: list[Request] = []
        self.followed_redirect = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers.get("Content-Length", "0"))
                fixture.requests.append(
                    Request(
                        self.path,
                        {name.lower(): value for name, value in self.headers.items()},
                        self.rfile.read(size),
                    )
                )
                if fixture.delay:
                    time.sleep(fixture.delay)
                self.send_response(fixture.status)
                if 300 <= fixture.status < 400:
                    self.send_header("Location", fixture.base_url + "/redirected")
                else:
                    self.send_header("Content-Type", "application/json")
                self.end_headers()
                if fixture.raw:
                    encoded = bytes(fixture.response)
                else:
                    encoded = json.dumps(fixture.response).encode()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self) -> None:
                fixture.followed_redirect = True
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "version": 1,
                            "kind": "qText",
                            "text": "redirect followed",
                        }
                    ).encode()
                )

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host, self.port = self._server.server_address[:2]
        self.base_url = f"http://{self.host}:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> BrokerServer:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def received_bearer(self, token: str) -> bool:
        return bool(
            self.requests and self.requests[0].headers.get("authorization") == f"Bearer {token}"
        )


def test_broker_posts_authenticated_versioned_request_and_decodes_table() -> None:
    response = {
        "version": 1,
        "kind": "table",
        "columns": ["sym", "price"],
        "rows": [["AAPL", 224.1], ["MSFT", 518.0]],
        "rowCount": 20,
        "label": "local broker",
    }
    fake_token = "fixture-broker-token"
    with BrokerServer(response) as server:
        evaluator = BrokerEvaluator(server.base_url + "/", fake_token, timeout=2)
        result = evaluator.evaluate(
            "select from trade",
            EvaluationContext(row_limit=2, byte_limit=20_000, timeout=0.75),
        )

    assert isinstance(result, EvaluationResult)
    assert result.value == [["AAPL", 224.1], ["MSFT", 518.0]]
    assert list(result.columns or ()) == ["sym", "price"]
    assert result.row_count == 20
    assert result.label == "local broker"
    assert server.received_bearer(fake_token)
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request.path == "/v1/evaluate"
    assert request.headers["content-type"].startswith("application/json")
    assert request.headers["accept"] == "application/json"
    assert json.loads(request.body) == {
        "version": 1,
        "source": "select from trade",
        "limits": {"rows": 2, "bytes": 20_000},
        "timeoutSeconds": 0.75,
    }


def test_broker_decodes_qtext_without_fabricating_completeness() -> None:
    response = {
        "version": 1,
        "kind": "qText",
        "text": "unsupported value preview",
        "truncated": True,
    }
    with BrokerServer(response) as server:
        result = BrokerEvaluator(server.base_url, "fixture-token").evaluate("value f")

    assert isinstance(result, EvaluationResult)
    assert isinstance(result.value, QText)
    assert result.value.text == "unsupported value preview"
    assert result.value.truncated is True


def test_broker_applies_default_context_when_none_is_supplied() -> None:
    response = {
        "version": 1,
        "kind": "qText",
        "text": "42",
    }
    with BrokerServer(response) as server:
        BrokerEvaluator(server.base_url, "fixture-token").evaluate("6*7")

    assert json.loads(server.requests[0].body) == {
        "version": 1,
        "source": "6*7",
        "limits": {"rows": 20, "bytes": 1_000_000},
    }


def test_broker_uses_shared_http_timeout_default_and_preserves_override() -> None:
    default = BrokerEvaluator("http://127.0.0.1:5000", "fixture-token")
    explicit = BrokerEvaluator("http://127.0.0.1:5000", "fixture-token", timeout=30.0)

    assert default.timeout == DEFAULT_QUERY_TIMEOUT_SECONDS == 1800.0
    assert explicit.timeout == 30.0


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://192.0.2.1:5000",
        "ftp://127.0.0.1:5000",
        "file:broker.sock",
        "http://user:password@127.0.0.1:5000",
    ],
)
def test_broker_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError, match="base_url|HTTP|loopback|credentials"):
        BrokerEvaluator(url, "fixture-token")


def test_broker_requires_a_nonempty_runtime_token_and_redacts_it() -> None:
    with pytest.raises(ValueError, match="token"):
        BrokerEvaluator("http://127.0.0.1:5000", "")

    fake_token = "fixture-token-that-must-not-leak"
    with BrokerServer(
        {"error": f"rejected {fake_token}"},
        status=401,
    ) as server:
        evaluator = BrokerEvaluator(server.base_url, fake_token)
        assert fake_token not in repr(evaluator)
        with pytest.raises(Exception) as captured:
            evaluator.evaluate("1+1")

    assert fake_token not in str(captured.value)
    assert fake_token not in repr(captured.value)
    assert captured.value.__context__ is None
    assert captured.value.__cause__ is None


def test_broker_rejects_redirects() -> None:
    with BrokerServer({}, status=302) as server:
        with pytest.raises(Exception, match="redirect|302|HTTP"):
            BrokerEvaluator(server.base_url, "fixture-token").evaluate("1+1")

    assert server.followed_redirect is False


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"version": 2, "kind": "qText", "text": "x"},
        {"version": True, "kind": "qText", "text": "x"},
        {"version": 1.0, "kind": "qText", "text": "x"},
        {"version": 1, "kind": "unknown", "text": "x"},
        {"version": 1, "kind": "qText", "text": 42},
        {
            "version": 1,
            "kind": "table",
            "columns": ["x"],
            "rows": [[1, 2]],
            "rowCount": 1,
        },
        {
            "version": 1,
            "kind": "table",
            "columns": ["x"],
            "rows": [[1], [2]],
            "rowCount": 1,
        },
    ],
)
def test_broker_rejects_malformed_or_inconsistent_response(response: Any) -> None:
    with BrokerServer(response) as server:
        with pytest.raises((TypeError, ValueError, RuntimeError)):
            BrokerEvaluator(server.base_url, "fixture-token").evaluate("1+1")


def test_broker_rejects_non_json_and_times_out_cleanly() -> None:
    with BrokerServer(b"not json", raw=True) as server:
        with pytest.raises(Exception, match="JSON|json|response"):
            BrokerEvaluator(server.base_url, "fixture-token").evaluate("1+1")

    with BrokerServer(
        {"version": 1, "kind": "qText", "text": "late"},
        delay=0.2,
    ) as server:
        with pytest.raises(Exception, match="timed out|timeout"):
            BrokerEvaluator(server.base_url, "fixture-token", timeout=0.03).evaluate("slow[]")


def test_broker_rejects_duplicate_json_keys() -> None:
    duplicate = b'{"version":1,"kind":"qText","text":"first","text":"second"}'
    with BrokerServer(duplicate, raw=True) as server:
        with pytest.raises(Exception, match="unsafe|invalid|JSON"):
            BrokerEvaluator(server.base_url, "fixture-token").evaluate("1+1")


def test_broker_repr_redacts_token_overlapping_its_url() -> None:
    secret = "fixture-secret"
    evaluator = BrokerEvaluator(f"http://127.0.0.1:5000/{secret}", secret)

    assert secret not in repr(evaluator)


def test_broker_verbose_traceback_locals_do_not_capture_runtime_token() -> None:
    secret = "trace-broker-token-7c2d"
    raw = b'{"version":1,"kind":"qText","text":"' + secret.encode() + b'","unexpected":true}'
    with BrokerServer(raw, raw=True) as server:
        evaluator = BrokerEvaluator(server.base_url, secret)
        try:
            evaluator.evaluate("1+1")
        except Exception as error:
            frames: list[str] = []
            current = error.__traceback__
            while current is not None:
                if "/src/kx_notebook/" in current.tb_frame.f_code.co_filename:
                    frames.append(repr(current.tb_frame.f_locals))
                current = current.tb_next
            captured = "\n".join(frames)
            context = error.__context__
            cause = error.__cause__
        else:  # pragma: no cover - the response is intentionally invalid
            pytest.fail("expected invalid broker response")

    assert secret not in captured
    assert context is None
    assert cause is None
