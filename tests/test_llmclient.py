"""Direct tests for llmclient's HTTP boundary: payload/response parsing,
token-stats mapping and error-field handling of generate_with_stats.
Everything else in the suite mocks this seam; these tests exercise it
with a faked urlopen."""

import json
import urllib.error

import pytest

from replicanta import llmclient


def _fake_resp(payload):
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    return FakeResp()


def _patch_urlopen(monkeypatch, payload):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=None: _fake_resp(payload)
    )


def test_generate_with_stats_maps_token_counts(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        {
            "response": "hello",
            "prompt_eval_count": 42,
            "eval_count": 7,
        },
    )
    text, stats = llmclient.generate_with_stats("prompt", "qwen2.5:3b", 5)
    assert text == "hello"
    assert stats == {"prompt_tokens": 42, "gen_tokens": 7}


def test_generate_with_stats_defaults_missing_counts_to_zero(monkeypatch):
    _patch_urlopen(monkeypatch, {"response": "hi"})
    _, stats = llmclient.generate_with_stats("prompt", "qwen2.5:3b", 5)
    assert stats == {"prompt_tokens": 0, "gen_tokens": 0}


def test_generate_with_stats_error_field_raises(monkeypatch):
    _patch_urlopen(monkeypatch, {"error": "model not found"})
    with pytest.raises(RuntimeError, match="model not found"):
        llmclient.generate_with_stats("prompt", "qwen2.5:3b", 5)


def test_generate_with_stats_strips_think_and_special(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        {
            "response": "<think>reasoning</think>answer<|im_start|>loop",
        },
    )
    text, _ = llmclient.generate_with_stats("prompt", "qwen2.5:3b", 5)
    assert text == "answer"


def test_generate_with_stats_url_error_propagates(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(urllib.error.URLError):
        llmclient.generate_with_stats("prompt", "qwen2.5:3b", 5)


def test_generate_with_stats_sends_expected_payload(monkeypatch):
    captured = {}

    def spy(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        return _fake_resp({"response": "ok"})

    monkeypatch.setattr("urllib.request.urlopen", spy)
    llmclient.generate_with_stats("p", "qwen2.5:3b", 9, temperature=0.3)
    assert captured["body"]["model"] == "qwen2.5:3b"
    assert captured["body"]["prompt"] == "p"
    assert captured["body"]["stream"] is False
    assert captured["body"]["options"]["temperature"] == 0.3
    assert captured["timeout"] == 9


# -- llama.cpp backend -------------------------------------------------------


def test_llama_cpp_generate_with_stats_maps_token_counts(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        {
            "content": "hello",
            "tokens_evaluated": 42,
            "tokens_predicted": 7,
        },
    )
    monkeypatch.setenv("REPLICANTA_LLM_BACKEND", "llama_cpp")
    text, stats = llmclient.generate_with_stats("prompt", "ignored", 5)
    assert text == "hello"
    assert stats == {"prompt_tokens": 42, "gen_tokens": 7}


def test_llama_cpp_generate_sends_expected_payload(monkeypatch):
    captured = {}

    def spy(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        return _fake_resp({"content": "ok"})

    monkeypatch.setattr("urllib.request.urlopen", spy)
    monkeypatch.setenv("REPLICANTA_LLM_BACKEND", "llama_cpp")
    monkeypatch.setenv("LLAMACPP_URL", "http://localhost:9999")
    llmclient.generate_with_stats("p", "any-model", 9, temperature=0.3)
    assert captured["url"] == "http://localhost:9999/completion"
    assert "model" not in captured["body"]
    assert captured["body"]["prompt"] == "p"
    assert captured["body"]["stream"] is False
    assert captured["body"]["n_predict"] == llmclient.MAX_TOKENS
    assert captured["body"]["temperature"] == 0.3
    assert captured["timeout"] == 9


def test_llama_cpp_probe_voice_checks_health(monkeypatch):
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        status = 200

        def read(self):
            return b"{}"

    def spy(req, timeout=None):
        captured["url"] = req.full_url
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", spy)
    monkeypatch.setenv("REPLICANTA_LLM_BACKEND", "llama_cpp")
    monkeypatch.setenv("LLAMACPP_URL", "http://localhost:9999")
    llmclient.reset_voice()
    assert llmclient.probe_voice() is True
    assert captured["url"] == "http://localhost:9999/health"


def test_backend_defaults_to_ollama():
    # Default backend should be ollama when env var is absent/unset.
    assert llmclient.llm_backend() == "ollama"


def test_describe_image_raises_on_llama_cpp_backend(monkeypatch):
    monkeypatch.setenv("REPLICANTA_LLM_BACKEND", "llama_cpp")
    with pytest.raises(RuntimeError, match="vision is not supported"):
        llmclient.describe_image(b"fake-image-bytes")
