"""Typed-decision client: decide() batches typed questions into one POST to
the arbiter server's /v1/systemone endpoint and returns the answers map.

Contract under test: ARBITER_URL unset means the client is inert (no
network, None returned); the request carries model "jev-latest", the
questions verbatim, and a Bearer token only when ARBITER_API_KEY is set;
any error — unreachable host, bad status, missing answers — returns None
after logging, and a failure starts a cool-down so a down server is not
re-contacted on every call."""

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from replicanta import typeddecisions


@pytest.fixture(autouse=True)
def _reset_cooldown():
    """The failure cool-down is module-global; isolate it between tests."""
    typeddecisions._mark_up()
    yield
    typeddecisions._mark_up()


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode())
        self.server.requests.append(
            {
                "path": self.path,
                "content_type": self.headers.get("Content-Type"),
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )
        if self.server.fail_with is not None:
            self.send_response(self.server.fail_with)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "boom"}}).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        answers = {qid: {"type": q["type"], "noul": 0.5, "confidence": 0.9} for qid, q in body["questions"].items()}
        self.wfile.write(
            json.dumps({"model": "jev-latest", "answers": answers, "usage": {}, "latency_ms": 3.0}).encode()
        )

    def log_message(self, *args):
        pass


@pytest.fixture
def stub_server(monkeypatch):
    """A localhost arbiter stand-in; resets the client cool-down around each test."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    server.fail_with = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ARBITER_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.delenv("ARBITER_API_KEY", raising=False)
    typeddecisions._mark_up()
    yield server
    typeddecisions._mark_up()
    server.shutdown()
    server.server_close()


# -- configuration ---------------------------------------------------------------


def test_decide_inert_without_arbiter_url(monkeypatch):
    """Unset ARBITER_URL is the default behavior everywhere: no network, None."""
    monkeypatch.delenv("ARBITER_URL", raising=False)

    def forbidden_urlopen(*a, **k):
        raise AssertionError("decide() must not touch the network when ARBITER_URL is unset")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_urlopen)
    assert typeddecisions.enabled() is False
    assert typeddecisions.decide("state", {"q": typeddecisions.q_noul("statement")}) is None


def test_enabled_and_defaults(monkeypatch):
    monkeypatch.delenv("ARBITER_URL", raising=False)
    assert typeddecisions.arbiter_url() == typeddecisions.DEFAULT_URL
    assert typeddecisions.timeout_ms() == typeddecisions.DEFAULT_TIMEOUT_MS
    monkeypatch.setenv("ARBITER_URL", "http://example.local:8010/")
    assert typeddecisions.enabled() is True
    assert typeddecisions.arbiter_url() == "http://example.local:8010"  # trailing slash stripped


# -- wire contract -----------------------------------------------------------------


def test_decide_posts_systemone_contract(stub_server):
    answers = typeddecisions.decide(
        "the state text",
        {
            "n": typeddecisions.q_noul("a statement"),
            "c": typeddecisions.q_choice("pick one", {"a": "first", "b": "second"}),
            "s": typeddecisions.q_score("how much", ["low", "high"]),
        },
    )
    assert answers is not None
    (request,) = stub_server.requests
    assert request["path"] == "/v1/systemone"
    assert request["content_type"] == "application/json"
    assert request["body"]["model"] == "jev-latest"
    assert request["body"]["state"] == "the state text"
    assert request["body"]["questions"]["n"] == {"type": "noul", "instructions": "a statement"}
    assert request["body"]["questions"]["c"]["criteria"] == {"a": "first", "b": "second"}
    assert request["body"]["questions"]["s"]["criteria"] == ["low", "high"]
    assert request["authorization"] is None  # no key configured -> no header


def test_decide_sends_bearer_token_when_key_set(stub_server, monkeypatch):
    monkeypatch.setenv("ARBITER_API_KEY", "secret-key")
    typeddecisions.decide("state", {"q": typeddecisions.q_noul("statement")})
    (request,) = stub_server.requests
    assert request["authorization"] == "Bearer secret-key"


# -- failure semantics --------------------------------------------------------------


def test_decide_returns_none_on_http_error(stub_server):
    stub_server.fail_with = 500
    assert typeddecisions.decide("state", {"q": typeddecisions.q_noul("statement")}) is None


def test_decide_returns_none_when_answers_missing(monkeypatch):
    """A 200 without an answers map is not a decision; fall back."""

    class _Resp:
        def read(self):
            return json.dumps({"model": "jev-latest"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setenv("ARBITER_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert typeddecisions.decide("state", {"q": typeddecisions.q_noul("statement")}) is None


def test_decide_returns_none_on_connection_error_and_cools_down(monkeypatch):
    """A down arbiter must not be re-contacted: the first failure starts a
    cool-down and the next decide() returns None without any network I/O."""
    monkeypatch.setenv("ARBITER_URL", "http://127.0.0.1:9")  # closed port
    calls = []

    def refusing_urlopen(*a, **k):
        calls.append(1)
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refusing_urlopen)
    assert typeddecisions.decide("state", {"q": typeddecisions.q_noul("statement")}) is None
    assert typeddecisions.decide("state", {"q": typeddecisions.q_noul("statement")}) is None
    assert len(calls) == 1


# -- answer accessors ----------------------------------------------------------------


def test_accessors_read_typed_answers():
    answers = {
        "n": {"type": "noul", "noul": 0.7, "confidence": 0.9},
        "c": {"type": "choice", "choice": "b", "probabilities": {"a": 0.3, "b": 0.7}, "confidence": 0.8},
        "s": {
            "type": "score",
            "score": 2.4,
            "probabilities": {"0": 0.1, "1": 0.2, "2": 0.4, "3": 0.3},
            "confidence": 0.9,
        },
    }
    assert typeddecisions.noul_of(answers, "n") == 0.7
    assert typeddecisions.choice_of(answers, "c") == ("b", 0.7)
    assert typeddecisions.score_of(answers, "s") == 2.4


def test_accessors_swallow_missing_or_malformed_answers():
    answers = {"n": {"type": "noul"}, "c": {"type": "choice", "choice": "a"}, "s": {"type": "score", "score": "NaNish"}}
    assert typeddecisions.noul_of(answers, "n") is None
    assert typeddecisions.choice_of(answers, "c") == ("a", 0.0)  # no probabilities -> zero confidence
    assert typeddecisions.score_of(answers, "s") is None
    assert typeddecisions.noul_of(answers, "missing") is None
    assert typeddecisions.choice_of(answers, "missing") is None
    assert typeddecisions.score_of(answers, "missing") is None
