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
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _fake_resp(payload))


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
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 42, "completion_tokens": 7},
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
        return _fake_resp(
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", spy)
    monkeypatch.setenv("REPLICANTA_LLM_BACKEND", "llama_cpp")
    monkeypatch.setenv("LLAMACPP_URL", "http://localhost:9999")
    llmclient.generate_with_stats("p", "any-model", 9, temperature=0.3)
    assert captured["url"] == "http://localhost:9999/v1/chat/completions"
    assert "model" not in captured["body"]
    # the server-side chat template wraps the prompt; thinking is disabled
    # so reasoning models answer instead of burning the token budget
    assert captured["body"]["messages"] == [{"role": "user", "content": "p"}]
    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "prompt" not in captured["body"]
    assert captured["body"]["stream"] is False
    assert captured["body"]["n_predict"] == llmclient.MAX_TOKENS
    assert captured["body"]["temperature"] == 0.3
    assert captured["timeout"] == 9


def test_llama_cpp_retry_when_reply_empty_at_token_cap(monkeypatch):
    bodies = []

    def spy(req, timeout=None):
        bodies.append(json.loads(req.data.decode()))
        # first call: empty answer that hit the cap; second: a real answer
        payload = {
            "choices": [{"message": {"content": ""}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": llmclient.MAX_TOKENS},
        }
        if len(bodies) == 2:
            payload = {
                "choices": [{"message": {"content": "hi ada"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 40},
            }
        return _fake_resp(payload)

    monkeypatch.setattr("urllib.request.urlopen", spy)
    monkeypatch.setenv("REPLICANTA_LLM_BACKEND", "llama_cpp")
    monkeypatch.setenv("LLAMACPP_URL", "http://localhost:9999")
    text, stats = llmclient.generate_with_stats("p", "any-model", 9)
    assert text == "hi ada"
    assert len(bodies) == 2
    assert stats["gen_tokens"] == 40


def test_llama_cpp_no_retry_when_model_stopped_early(monkeypatch):
    calls = []

    def spy(req, timeout=None):
        calls.append(1)
        return _fake_resp(
            {
                "choices": [{"message": {"content": ""}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 50},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", spy)
    monkeypatch.setenv("REPLICANTA_LLM_BACKEND", "llama_cpp")
    monkeypatch.setenv("LLAMACPP_URL", "http://localhost:9999")
    text, _ = llmclient.generate_with_stats("p", "any-model", 9)
    assert text == ""
    assert len(calls) == 1


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


def test_mark_voice_offline_forces_state_offline():
    llmclient.reset_voice()
    assert llmclient.voice_online() is None
    llmclient.note_voice_success()
    assert llmclient.voice_online() is True
    llmclient.mark_voice_offline()
    assert llmclient.voice_online() is False
    assert llmclient.voice_status() == "offline"
    llmclient.reset_voice()


def test_describe_image_raises_on_llama_cpp_backend(monkeypatch):
    monkeypatch.setenv("REPLICANTA_LLM_BACKEND", "llama_cpp")
    with pytest.raises(RuntimeError, match="vision is not supported"):
        llmclient.describe_image(b"fake-image-bytes")


# -- SSRF hardening ----------------------------------------------------------


def test_ollama_url_rejects_metadata_endpoint(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://169.254.169.254/api/generate")
    with pytest.raises(ValueError, match="not allowed"):
        llmclient.ollama_url()


def test_ollama_url_rejects_non_http_scheme(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "file:///etc/passwd")
    with pytest.raises(ValueError, match="scheme must be http"):
        llmclient.ollama_url()


def test_ollama_url_rejects_embedded_credentials(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://user:pass@localhost:11434/api/generate")
    with pytest.raises(ValueError, match="must not contain credentials"):
        llmclient.ollama_url()


def test_llama_cpp_url_rejects_metadata_endpoint(monkeypatch):
    monkeypatch.setenv("LLAMACPP_URL", "http://169.254.169.254:8085")
    with pytest.raises(ValueError, match="not allowed"):
        llmclient.llama_cpp_url()


def test_llama_cpp_url_allows_localhost(monkeypatch):
    monkeypatch.setenv("LLAMACPP_URL", "http://localhost:8085")
    assert llmclient.llama_cpp_url() == "http://localhost:8085"
