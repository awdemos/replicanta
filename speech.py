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
"""

import io
import os
import queue
import re
import subprocess
import threading
import wave
from pathlib import Path

VOICES_DIR = Path(__file__).parent / "voices"
_DEFAULT_MODEL = VOICES_DIR / "en_US-lessac-medium.onnx"
_model_path = Path(os.environ.get("REPLICANTA_VOICE_MODEL") or _DEFAULT_MODEL)

HF_VOICE_URL = ("https://huggingface.co/rhasspy/piper-voices/resolve/"
                "v1.0.0/{lang}/{locale}/{name}/{quality}/{full}{ext}")
_VOICE_NAME_RE = re.compile(r"^([a-z]{2,3}_[A-Z]{2})-([a-z0-9_]+)-([a-z]+)$")

enabled = False
_queue = queue.Queue()
_worker = None
_voice = None
_voice_lock = threading.Lock()


def available():
    """True when a piper model file is present (packages may still be
    missing — failures are contained at speak time)."""
    return _model_path.exists()


def model_path():
    """Path of the active piper model (mutated by set_voice/reset)."""
    return _model_path


def voice_name():
    """Name of the active voice, e.g. 'en_US-lessac-medium'."""
    return _model_path.stem


def list_voices():
    """Names of all voices downloaded into voices/."""
    if not VOICES_DIR.exists():
        return []
    return sorted(p.stem for p in VOICES_DIR.glob("*.onnx"))


def set_voice(spec):
    """Switch the active voice. Accepts a bare voice name
    ('en_US-lessac-medium'), a filename ('en_US-lessac-medium.onnx') or
    an .onnx path. Returns the resolved path, or None if not found —
    in which case the current voice is kept."""
    global _model_path, _voice
    candidates = [Path(spec)]
    if not Path(spec).suffix:
        candidates.append(VOICES_DIR / f"{spec}.onnx")
    candidates.append(VOICES_DIR / spec)
    for cand in candidates:
        if cand.suffix == ".onnx" and cand.exists():
            _model_path = cand
            _voice = None          # next speak loads the new model
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
        HF_VOICE_URL.format(lang=lang, locale=locale, name=name,
                            quality=quality, full=full, ext=ext)
        for ext in (".onnx", ".onnx.json"))


def download_voice(spec):
    """Fetch a piper voice (model + config) into voices/ from the
    rhasspy/piper-voices HuggingFace repo. Returns the model path, or
    None when the name is invalid or the download fails. Uses curl —
    the container's python ssl can't verify huggingface certs."""
    urls = voice_urls(spec)
    if urls is None:
        return None
    VOICES_DIR.mkdir(exist_ok=True)
    model = VOICES_DIR / f"{spec}.onnx"
    for url, dest in zip(urls, (model, VOICES_DIR / f"{spec}.onnx.json")):
        try:
            subprocess.run(
                ["curl", "-sfSL", "-o", str(dest), url],
                check=True, timeout=300)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                OSError):
            model.unlink(missing_ok=True)     # don't leave half a voice
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
        _worker = threading.Thread(
            target=_drain, daemon=True, name="speech")
        _worker.start()
    _queue.put(text)


def _drain():
    while True:
        try:
            text = _queue.get(timeout=30)
        except queue.Empty:
            return
        try:
            _speak(text)
        except Exception:  # noqa: BLE001, S110 — speech must never kill anything
            pass


def _load_voice():
    global _voice
    with _voice_lock:
        if _voice is None:
            from piper import PiperVoice
            _voice = PiperVoice.load(str(_model_path))
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
    sc.default_speaker().play(pcm, samplerate=rate)


def reset():
    """Test hook: drop the flag, cached voice, voice choice and queue."""
    global enabled, _voice, _model_path
    enabled = False
    _voice = None
    _model_path = Path(os.environ.get("REPLICANTA_VOICE_MODEL")
                      or _DEFAULT_MODEL)
    while True:
        try:
            _queue.get_nowait()
        except queue.Empty:
            return
