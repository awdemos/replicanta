"""Spoken voice: piper TTS + PulseAudio playback.

Entirely optional. The piper model, the piper-tts and soundcard packages
are all imported lazily — if any piece is missing, every say() is a
silent no-op and the organism keeps working in text. Speech runs on one
daemon thread draining a queue so utterances never overlap, never block
the UI, and can never take the app down.

Model: voices/en_US-lessac-medium.onnx (override with
REPLICANTA_VOICE_MODEL). Playback goes through PulseAudio/PipeWire, so
it reaches the host's sound server even inside a Toolbx container.
"""

import io
import os
import queue
import threading
import wave
from pathlib import Path

MODEL_PATH = Path(
    os.environ.get("REPLICANTA_VOICE_MODEL")
    or Path(__file__).parent / "voices" / "en_US-lessac-medium.onnx")

enabled = False
_queue = queue.Queue()
_worker = None
_voice = None
_voice_lock = threading.Lock()


def available():
    """True when a piper model file is present (packages may still be
    missing — failures are contained at speak time)."""
    return MODEL_PATH.exists()


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
            _voice = PiperVoice.load(str(MODEL_PATH))
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
    """Test hook: drop the flag, cached voice and any queued speech."""
    global enabled, _voice
    enabled = False
    _voice = None
    while True:
        try:
            _queue.get_nowait()
        except queue.Empty:
            return
