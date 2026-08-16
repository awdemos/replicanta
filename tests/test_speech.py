"""Spoken voice (speech.py): the piper TTS path is optional and contained —
disabled by default, no-ops without a model, serializes utterances on one
daemon thread, and swallows synthesis/playback failures so speech can
never take the organism down. Piper and soundcard are never imported in
these tests: _speak is patched out."""

import threading
import time
from pathlib import Path

from replicanta import speech


def _drain_until_empty(timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if speech._queue.empty():
            return True
        time.sleep(0.01)
    return False


def test_speech_disabled_by_default(monkeypatch):
    called = []
    monkeypatch.setattr(speech, "_speak", lambda text: called.append(text))
    monkeypatch.setattr(speech, "available", lambda: True)
    speech.say("hello")
    assert called == []
    assert speech._queue.empty()


def test_speech_noop_without_model(monkeypatch):
    called = []
    monkeypatch.setattr(speech, "_speak", lambda text: called.append(text))
    monkeypatch.setattr(speech, "available", lambda: False)
    speech.set_enabled(True)
    speech.say("hello")
    assert called == []
    assert speech._queue.empty()


def test_speech_speaks_when_enabled(monkeypatch):
    done = threading.Event()
    said = []

    def fake_speak(text):
        said.append(text)
        if len(said) == 2:
            done.set()

    monkeypatch.setattr(speech, "_speak", fake_speak)
    monkeypatch.setattr(speech, "available", lambda: True)
    speech.set_enabled(True)
    speech.say("first utterance")
    speech.say("second utterance")
    assert done.wait(2.0)
    # one thread, queued in order — utterances never overlap
    assert said == ["first utterance", "second utterance"]


def test_speech_failures_are_contained(monkeypatch):
    done = threading.Event()
    said = []

    def flaky_speak(text):
        if text == "boom":
            raise RuntimeError("piper exploded")
        said.append(text)
        done.set()

    monkeypatch.setattr(speech, "_speak", flaky_speak)
    monkeypatch.setattr(speech, "available", lambda: True)
    speech.set_enabled(True)
    speech.say("boom")
    speech.say("still alive")
    assert done.wait(2.0)
    assert said == ["still alive"]  # failure didn't kill the worker


def test_speech_ignores_empty_text(monkeypatch):
    called = []
    monkeypatch.setattr(speech, "_speak", lambda text: called.append(text))
    monkeypatch.setattr(speech, "available", lambda: True)
    speech.set_enabled(True)
    speech.say("")
    speech.say(None)
    assert called == []
    assert speech._queue.empty()


# -- voice management: list / switch / download ---------------------------


def _fake_voices_dir(tmp_path, monkeypatch, names=("en_US-lessac-medium",)):
    vdir = tmp_path / "voices"
    vdir.mkdir()
    for n in names:
        (vdir / f"{n}.onnx").write_text("fake")
        (vdir / f"{n}.onnx.json").write_text("{}")
    monkeypatch.setattr(speech, "VOICES_DIR", vdir)
    return vdir


def test_list_voices_scans_voices_dir(tmp_path, monkeypatch):
    _fake_voices_dir(tmp_path, monkeypatch, ("en_US-lessac-medium", "en_GB-alan-low"))
    assert speech.list_voices() == ["en_GB-alan-low", "en_US-lessac-medium"]


def test_list_voices_empty_without_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(speech, "VOICES_DIR", tmp_path / "nope")
    assert speech.list_voices() == []


def test_set_voice_by_name_filename_and_path(tmp_path, monkeypatch):
    vdir = _fake_voices_dir(tmp_path, monkeypatch, ("en_GB-alan-low",))
    assert speech.set_voice("en_GB-alan-low") == vdir / "en_GB-alan-low.onnx"
    assert speech.voice_name() == "en_GB-alan-low"
    assert speech.set_voice("en_GB-alan-low.onnx") is not None
    assert speech.set_voice(str(vdir / "en_GB-alan-low.onnx")) is not None


def test_set_voice_unknown_keeps_current(tmp_path, monkeypatch):
    _fake_voices_dir(tmp_path, monkeypatch)
    before = speech.model_path()
    assert speech.set_voice("en_GB-nope-low") is None
    assert speech.model_path() == before  # unchanged


def test_set_voice_drops_cached_model(tmp_path, monkeypatch):
    _fake_voices_dir(tmp_path, monkeypatch, ("en_GB-alan-low",))
    speech._voice = object()  # pretend a model is loaded
    speech.set_voice("en_GB-alan-low")
    assert speech._voice is None  # reloads on next speak


def test_voice_urls_parses_hf_layout():
    model, config = speech.voice_urls("en_US-lessac-medium")
    assert model == (
        "https://huggingface.co/rhasspy/piper-voices/resolve/"
        "v1.0.0/en/en_US/lessac/medium/"
        "en_US-lessac-medium.onnx"
    )
    assert config.endswith("en_US-lessac-medium.onnx.json")
    model, _ = speech.voice_urls("en_US-libritts_r-medium")
    assert "/en_US/libritts_r/medium/en_US-libritts_r-medium.onnx" in model


def test_voice_urls_rejects_bad_names():
    assert speech.voice_urls("lessac") is None
    assert speech.voice_urls("../etc/passwd") is None
    assert speech.voice_urls("EN_US-lessac-medium") is None
    # Prefix-valid spec embedding traversal must not reach curl -o.
    assert speech.voice_urls("en_US-x/../../../tmp/evil-low") is None
    assert speech.voice_urls("en_US-x/../evil-low") is None


def test_download_voice_invalid_name_never_calls_curl(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("curl must not run for an invalid name")

    monkeypatch.setattr(speech.subprocess, "run", boom)
    assert speech.download_voice("not a voice") is None


def test_download_voice_success(tmp_path, monkeypatch):
    _fake_voices_dir(tmp_path, monkeypatch, ())
    written = []

    def fake_run(cmd, check, timeout):
        written.append(cmd[cmd.index("-o") + 1])
        Path(written[-1]).write_text("fake")

    monkeypatch.setattr(speech.subprocess, "run", fake_run)
    model = speech.download_voice("en_GB-alan-low")
    assert model == tmp_path / "voices" / "en_GB-alan-low.onnx"
    assert len(written) == 2  # model + config


def test_download_voice_curl_failure_cleans_up(tmp_path, monkeypatch):
    import subprocess

    vdir = _fake_voices_dir(tmp_path, monkeypatch, ())

    def failing(cmd, check, timeout):
        raise subprocess.CalledProcessError(22, cmd)

    monkeypatch.setattr(speech.subprocess, "run", failing)
    assert speech.download_voice("en_GB-alan-low") is None
    assert list(vdir.glob("*.onnx")) == []  # no half-downloaded voice


# -- voices directory resolution ---------------------------------------------


def test_voices_dir_prefers_project_root(tmp_path, monkeypatch):
    """After the src-layout migration, voices/ lives at the project root,
    not under src/replicanta/. The module must find the root directory."""
    root = tmp_path / "project"
    src_replicanta = root / "src" / "replicanta"
    src_replicanta.mkdir(parents=True)
    package_voices = src_replicanta / "voices"
    package_voices.mkdir()
    root_voices = root / "voices"
    root_voices.mkdir()

    fake_speech = root / "src" / "replicanta" / "speech.py"
    fake_speech.write_text(Path(speech.__file__).read_text())

    import importlib.util

    spec = importlib.util.spec_from_file_location("fake_speech", fake_speech)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.VOICES_DIR == root_voices


def test_voices_dir_falls_back_to_package_dir(tmp_path, monkeypatch):
    """When no project-root voices/ exists, use the package directory."""
    root = tmp_path / "project"
    src_replicanta = root / "src" / "replicanta"
    src_replicanta.mkdir(parents=True)
    package_voices = src_replicanta / "voices"
    package_voices.mkdir()

    fake_speech = root / "src" / "replicanta" / "speech.py"
    fake_speech.write_text(Path(speech.__file__).read_text())

    import importlib.util

    spec = importlib.util.spec_from_file_location("fake_speech2", fake_speech)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.VOICES_DIR == package_voices
