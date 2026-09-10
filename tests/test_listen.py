"""Heard-voice feature: push-to-talk Listener (start/stop/toggle state
machine, mono conversion, transcription gating) and the TUI /listen
wiring. No real microphone or whisper model is touched."""

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from replicanta import listen
from replicanta.listen import Listener


def _audio(seconds=1.0, value=0.1):
    return np.full(int(listen.SAMPLE_RATE * seconds), value, dtype=np.float32)


# -- _mono --------------------------------------------------------------------


def test_mono_averages_channels():
    frames = np.array([[0.5, 1.0], [-0.5, 0.5]], dtype=np.float32)
    out = listen._mono(frames)
    assert out.shape == (2,)
    assert out.dtype == np.float32
    assert out[0] == pytest.approx(0.75)
    assert out[1] == pytest.approx(0.0)


def test_mono_passes_through_1d():
    out = listen._mono(np.ones(4, dtype=np.float32))
    assert out.shape == (4,)


# -- toggle state machine ------------------------------------------------------


class _FakeMic:
    """Silence-generating stand-in for a soundcard microphone."""

    def recorder(self, samplerate):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def record(self, numframes):
        return np.zeros((numframes, 1), dtype=np.float32)


def _listener(**kwargs):
    kwargs.setdefault("mic_factory", _FakeMic)
    return Listener(**kwargs)


def test_toggle_starts_then_stops():
    li = _listener()
    recording, audio = li.toggle()
    assert recording is True and audio is None
    assert li.recording is True
    recording, audio = li.toggle()
    assert recording is False
    assert li.recording is False
    assert isinstance(audio, np.ndarray)
    assert len(audio) > 0  # the fake mic really captured frames


def test_start_is_idempotent():
    li = _listener()
    li.start()
    first = li._thread
    li.start()  # must not spawn a second capture
    assert li._thread is first
    li.stop()


def test_no_microphone_is_a_quiet_noop():
    def dead_mic():
        raise OSError("no such device")

    li = Listener(mic_factory=dead_mic)
    li.start()
    assert li.recording is False  # recording never began
    assert len(li.stop()) == 0


def test_stop_without_start_returns_empty():
    li = Listener()
    audio = li.stop()
    assert len(audio) == 0


# -- transcription ----------------------------------------------------------


def test_transcribe_uses_injected_transcriber():
    seen = []
    li = Listener(transcriber=lambda a: seen.append(len(a)) or " hello ")
    assert li.transcribe(_audio(1.0)) == "hello"
    assert seen == [listen.SAMPLE_RATE]


def test_transcribe_rejects_too_short_audio():
    li = Listener(transcriber=lambda _a: "should not happen")
    assert li.transcribe(_audio(0.05)) == ""
    assert li.transcribe(None) == ""


def test_transcribe_contains_transcriber_failures():
    def boom(_a):
        raise RuntimeError("model exploded")

    li = Listener(transcriber=boom)
    assert li.transcribe(_audio(1.0)) == ""


# -- TUI wiring ---------------------------------------------------------------


class _FakeListener:
    """Scriptable stand-in: toggles like Listener, no mic, no whisper."""

    def __init__(self):
        self.recording = False
        self.stopped = False

    def start(self):
        self.recording = True

    def stop(self):
        self.recording = False
        self.stopped = True
        return _audio(1.0)

    def transcribe(self, _audio):
        return ""


def test_listen_command_toggles_mic(tmp_path):
    from replicanta.organism import Organism
    from replicanta.tui import OrganismApp

    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    fake = _FakeListener()
    app.listener = fake
    app.handle_command("/listen")
    assert fake.recording is True
    app._transcribe_then_say = lambda _audio: None  # no worker in tests
    app.handle_command("/listen")
    assert fake.recording is False
    assert fake.stopped is True


# -- device matching (/microphone) ---------------------------------------------


def _mics():
    return [
        SimpleNamespace(id="alsa-0", name="Built-in Audio Analog Stereo"),
        SimpleNamespace(id="usb-1", name="Blue Yeti Microphone"),
    ]


def test_match_by_exact_id():
    assert listen.match_microphone(_mics(), "usb-1").name == "Blue Yeti Microphone"


def test_match_by_name_substring_case_insensitive():
    assert listen.match_microphone(_mics(), "yeti").id == "usb-1"
    assert listen.match_microphone(_mics(), "BUILT-IN").id == "alsa-0"


def test_match_prefers_exact_id_over_substring():
    mics = _mics() + [SimpleNamespace(id="usb", name="usb-1 clone")]
    assert listen.match_microphone(mics, "usb-1").id == "usb-1"


def test_match_nothing():
    assert listen.match_microphone(_mics(), "webcam") is None


def test_set_mic_with_factory_stores_spec():
    li = Listener(mic_factory=_FakeMic)
    assert li.set_mic("usb-1") == "usb-1"
    assert li.mic_spec == "usb-1"


def test_list_microphones_never_raises(monkeypatch):
    """A soundcard that explodes at call time yields [], not a crash.
    (The fake goes into sys.modules: importing the real soundcard needs
    libpulse, which CI containers don't have.)"""

    def boom(*args, **kwargs):
        raise OSError("no server")

    monkeypatch.setitem(sys.modules, "soundcard", SimpleNamespace(all_microphones=boom))
    assert listen.list_microphones() == []


def test_list_microphones_shapes_devices(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "soundcard",
        SimpleNamespace(all_microphones=lambda include_loopback=False: _mics()),
    )
    assert listen.list_microphones() == [
        ("alsa-0", "Built-in Audio Analog Stereo"),
        ("usb-1", "Blue Yeti Microphone"),
    ]


def test_microphone_command_status_and_use(tmp_path):
    from replicanta.organism import Organism
    from replicanta.tui import OrganismApp

    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    app.listener = Listener(mic_factory=_FakeMic)
    app.handle_command("/microphone")  # status line, no crash
    app.handle_command("/microphone use usb-1")
    assert app.listener.mic_spec == "usb-1"
