"""Spoken voice: piper TTS + PulseAudio playback.

Entirely optional. The piper model, the piper-tts and soundcard packages
are all imported lazily — if any piece is missing, every say() is a
silent no-op and the organism keeps working in text. Speech runs on one
daemon thread draining a queue so utterances never overlap, never block
the UI, and can never take the app down.

Voices live in voices/ as <name>.onnx + <name>.onnx.json pairs; any
piper voice from huggingface.co/rhasspy/piper-voices works. set_voice()
switches among downloaded ones, download_voice() fetches a new one.
REPLICANTA_VOICE_MODEL overrides the default starting model. Playback
goes through PulseAudio/PipeWire, so it reaches the host's sound server
even inside a Toolbx container.

Deliberate shape: unlike camera.py/listen.py (classes with injectable
seams), speech is a module-level singleton — one sound server, one
voice, one queue per process. reset() is the test hook.
"""

import io
import os
import queue
import re
import subprocess  # nosec
import threading
import wave
from pathlib import Path


def _find_voices_dir():
    """Locate the voices directory: prefer the project root (development
    src-layout), fall back to the package directory (installed wheel)."""
    candidates = [
        Path(__file__).parent.parent.parent / "voices",
        Path(__file__).parent / "voices",
    ]
    for cand in candidates:
        if cand.is_dir():
            return cand
    return candidates[0]


VOICES_DIR = None
_VOICES_DIR_CACHE = None


def voices_dir():
    """Return the voices directory, resolving it lazily on first call."""
    global _VOICES_DIR_CACHE
    if _VOICES_DIR_CACHE is None:
        _VOICES_DIR_CACHE = _find_voices_dir()
    return _VOICES_DIR_CACHE


_model_path = None


def _env_model_path():
    """Return the REPLICANTA_VOICE_MODEL override, or None."""
    env = os.environ.get("REPLICANTA_VOICE_MODEL")
    return Path(env) if env else None

HF_VOICE_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/"
    "v1.0.0/{lang}/{locale}/{name}/{quality}/{full}{ext}"
)
_VOICE_NAME_RE = re.compile(r"^([a-z]{2,3}_[A-Z]{2})-([a-z0-9_]+)-([a-z]+)$")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")

# Long model replies can take ages to synthesize and occasionally hang the
# audio backend; cap spoken output so voice stays responsive.
_MAX_SPEECH_CHARS = 280

# Piper emits a small amount of leading silence, but some PulseAudio/PipeWire
# sinks and Bluetooth DACs need a longer wake-up period. Prepend a short
# silent buffer so the first word isn't clipped.
_SPEECH_PREROLL_SECONDS = 0.25
# A matching tail buffer keeps the audio backend from trimming the end of the
# utterance while the stream is still draining.
_SPEECH_POSTROLL_SECONDS = 0.1

enabled = False
_queue = queue.Queue()
_worker = None
_voice = None
_voice_lock = threading.Lock()


def available():
    """True when a piper model file is present (packages may still be
    missing — failures are contained at speak time)."""
    return model_path().exists()


def model_path():
    """Path of the active piper model (env override > set_voice > default)."""
    env = _env_model_path()
    if env is not None:
        return env
    global _model_path
    if _model_path is None:
        _model_path = voices_dir() / "en_US-lessac-medium.onnx"
    return _model_path


def voice_name():
    """Name of the active voice, e.g. 'en_US-lessac-medium'."""
    return model_path().stem


def list_voices():
    """Names of all voices downloaded into voices/."""
    vdir = voices_dir()
    if not vdir.exists():
        return []
    return sorted(p.stem for p in vdir.glob("*.onnx"))


def set_voice(spec):
    """Switch the active voice. Accepts a bare voice name
    ('en_US-lessac-medium'), a filename ('en_US-lessac-medium.onnx') or
    an .onnx path inside voices/. Returns the resolved path, or None if
    not found — in which case the current voice is kept."""
    global _model_path, _voice
    vdir = voices_dir()
    candidates = [Path(spec)]
    if not Path(spec).suffix:
        candidates.append(vdir / f"{spec}.onnx")
    candidates.append(vdir / spec)
    vdir_resolved = vdir.resolve()
    for cand in candidates:
        if cand.suffix == ".onnx" and cand.exists():
            if not cand.resolve().is_relative_to(vdir_resolved):
                return None
            _model_path = cand
            _voice = None  # next speak loads the new model
            return cand
    return None


def voice_urls(spec):
    """HuggingFace URLs (model + config) for a piper voice name like
    'en_US-lessac-medium', or None when the name doesn't parse."""
    m = _VOICE_NAME_RE.match(spec)
    if not m:
        return None
    locale, name, quality = m.groups()
    lang = locale.split("_")[0]
    full = f"{locale}-{name}-{quality}"
    return tuple(
        HF_VOICE_URL.format(
            lang=lang, locale=locale, name=name, quality=quality, full=full, ext=ext
        )
        for ext in (".onnx", ".onnx.json")
    )


def download_voice(spec):
    """Fetch a piper voice (model + config) into voices/ from the
    rhasspy/piper-voices HuggingFace repo. Returns the model path, or
    None when the name is invalid or the download fails. Uses curl —
    the container's python ssl can't verify huggingface certs."""
    urls = voice_urls(spec)
    if urls is None:
        return None
    vdir = voices_dir()
    vdir.mkdir(exist_ok=True)
    model = vdir / f"{spec}.onnx"
    vdir_resolved = vdir.resolve()
    for dest in (model, vdir / f"{spec}.onnx.json"):
        if not dest.resolve().is_relative_to(vdir_resolved):
            return None
    for url, dest in zip(urls, (model, vdir / f"{spec}.onnx.json")):
        try:
            subprocess.run(  # nosec
                ["curl", "-sfSL", "-o", str(dest), url], check=True, timeout=300
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            model.unlink(missing_ok=True)  # don't leave half a voice
            return None
    return model


def set_enabled(on):
    global enabled
    enabled = bool(on)


def say(text):
    """Queue text to be spoken aloud. No-op unless enabled with a model
    present; never raises, never blocks the caller."""
    if not enabled or not text or not available():
        return
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_drain, daemon=True, name="speech")
        _worker.start()
    _queue.put(text)


def _trim_for_speech(text):
    """Keep spoken replies concise: first sentence, capped at a max length.
    Falls back to a hard truncation with ellipsis when no sentence boundary
    fits."""
    if len(text) <= _MAX_SPEECH_CHARS:
        return text
    window_start = _MAX_SPEECH_CHARS // 2
    window_end = _MAX_SPEECH_CHARS
    m = _SENTENCE_END_RE.search(text[window_start:window_end])
    if m:
        return text[: window_start + m.end()].strip()
    return text[:_MAX_SPEECH_CHARS].rstrip() + "..."


def _drain():
    while True:
        try:
            text = _queue.get(timeout=30)
        except queue.Empty:
            return
        _speak_with_timeout(_trim_for_speech(text))


def _speak_with_timeout(text, timeout=30):
    """Run _speak in a daemon thread and abandon it if playback/synthesis hangs.
    This keeps a single hung utterance from silencing every subsequent one."""
    done = []

    def target():
        try:
            _speak(text)
        except Exception:  # noqa: BLE001, S110 # nosec — speech must never kill anything
            pass
        done.append(True)

    t = threading.Thread(target=target, daemon=True, name="speech-utterance")
    t.start()
    t.join(timeout=timeout)


def _load_voice():
    global _voice
    with _voice_lock:
        if _voice is None:
            from piper import PiperVoice

            _voice = PiperVoice.load(str(model_path()))
    return _voice


def _speak(text):
    import numpy as np
    import soundcard as sc

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        _load_voice().synthesize_wav(text, w)
    buf.seek(0)
    with wave.open(buf, "rb") as w:
        rate, channels = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        pcm = pcm.reshape(-1, channels)
    # Book-end silence gives the audio backend and DAC time to start the
    # stream before speech begins and keeps the sink from trimming the tail.
    def _silence(seconds):
        frames = int(rate * seconds)
        if channels > 1:
            return np.zeros((frames, channels), dtype=np.float32)
        return np.zeros(frames, dtype=np.float32)

    pcm = np.concatenate(
        [_silence(_SPEECH_PREROLL_SECONDS), pcm, _silence(_SPEECH_POSTROLL_SECONDS)]
    )
    sc.default_speaker().play(pcm, samplerate=rate)


def reset():
    """Test hook: drop the flag, cached voice, voice choice and queue."""
    global enabled, _voice, _model_path
    enabled = False
    _voice = None
    _model_path = _env_model_path()
    while True:
        try:
            _queue.get_nowait()
        except queue.Empty:
            return
