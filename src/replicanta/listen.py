"""Heard voice: microphone -> faster-whisper STT -> the organism's ear.

Entirely optional, mirroring speech.py: faster-whisper and the mic are
imported lazily, failures are contained (a missing mic or package means
'nothing heard', never a crash), and capture runs on a daemon thread so
the UI never blocks. Push-to-talk only: start() begins capturing,
stop() ends it and returns the audio, transcribe() turns it into text.
The TUI feeds that text to the organism exactly like a typed line
(org.hear) — the organism cannot tell speech from typing.

Model and device are env-tunable: REPLICANTA_STT_MODEL (default 'base'),
REPLICANTA_STT_DEVICE (default 'cpu' — the GPU is usually busy running
the narrator's LLM), REPLICANTA_STT_COMPUTE (default 'int8'). The model
downloads from HuggingFace on first use (~150 MB for base).
"""

import os
import threading

SAMPLE_RATE = 16000


def _stt_config():
    """(model, device, compute) for WhisperModel, read per call so the
    REPLICANTA_STT_* env overrides are not frozen at import."""
    return (
        os.environ.get("REPLICANTA_STT_MODEL", "base"),
        os.environ.get("REPLICANTA_STT_DEVICE", "cpu"),
        os.environ.get("REPLICANTA_STT_COMPUTE", "int8"),
    )


MIN_SECONDS = 0.25  # shorter captures are treated as accidental taps


_resource_tracker_primed = False


def _ensure_resource_tracker():
    """Start multiprocessing's resource tracker on the main thread.

    faster-whisper's model load renders a tqdm progress bar (even for
    cached models); tqdm lazily creates a multiprocessing.RLock, which
    spawns the resource tracker subprocess via fork_exec. Inside the TUI
    that first spawn happens on a worker thread, where it can fail with
    "ValueError: bad value(s) in fds_to_keep" and transcription silently
    returns ''. Creating a throwaway RLock on the main thread at first
    Listener use leaves the tracker running, so the later worker-thread
    lock is a plain pipe write instead of a spawn.
    """
    global _resource_tracker_primed
    if _resource_tracker_primed:
        return
    try:
        import multiprocessing

        multiprocessing.RLock()
    except Exception:  # noqa: BLE001, S110 # nosec B110 — priming must never break startup
        pass
    _resource_tracker_primed = True


def match_microphone(mics, spec):
    """The first mic whose id equals spec exactly, else the first whose
    name contains it (case-insensitive); None when nothing matches."""
    for mic in mics:
        if mic.id == spec:
            return mic
    needle = spec.lower()
    for mic in mics:
        if needle in mic.name.lower():
            return mic
    return None


def list_microphones():
    """[(id, name)] of the host's input devices; [] when soundcard or the
    sound server is unavailable."""
    try:
        import soundcard as sc

        return [(m.id, m.name) for m in sc.all_microphones(include_loopback=False)]
    except Exception:  # noqa: BLE001 — listing must never kill anything
        return []


def _mono(frames):
    """soundcard frames ((n, channels) float32) -> mono 1-D float32."""
    import numpy as np

    audio = np.asarray(frames, dtype=np.float32)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    return np.ascontiguousarray(audio, dtype=np.float32)


class Listener:
    """Push-to-talk microphone -> text. Capture and transcription are
    separate calls so the caller decides which thread pays the (slow)
    STT cost. `transcriber` and `mic_factory` are injectable for tests.
    The microphone itself is resolved on the caller's thread in start()
    — first-time soundcard/cffi imports must not happen on the capture
    thread (import-time GC can drop pyo3 objects on the wrong thread)."""

    def __init__(self, transcriber=None, mic_factory=None):
        _ensure_resource_tracker()
        self._transcriber = transcriber
        self._mic_factory = mic_factory
        self.mic_spec = None  # chosen input device (id or name substring)
        self._thread = None
        self._chunks = []
        self._stop = threading.Event()
        self._model = None
        self._model_lock = threading.Lock()

    @property
    def recording(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        """Begin capturing. No-op when already recording, and a quiet
        no-op when no microphone can be opened (recording stays False)."""
        if self.recording:
            return
        try:
            mic = self._open_mic()
        except Exception:  # noqa: BLE001 — no mic / busy device
            return
        self._chunks = []
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._capture, args=(mic,), daemon=True, name="listen"
        )
        self._thread.start()

    def _open_mic(self):
        if self._mic_factory is not None:
            return self._mic_factory()
        import soundcard as sc

        if self.mic_spec is None:
            return sc.default_microphone()
        mic = match_microphone(
            sc.all_microphones(include_loopback=False), self.mic_spec
        )
        if mic is None:
            raise LookupError(f"no microphone matching {self.mic_spec!r}")
        return mic

    def set_mic(self, spec):
        """Choose the input device for future captures (exact id or name
        substring).

        When using real hardware, returns the matched device name and raises
        LookupError when nothing matches. When a mic_factory is injected,
        stores and returns the resolved spec without matching.
        """
        if self._mic_factory is not None:
            self.mic_spec = spec
            return spec
        import soundcard as sc

        mic = match_microphone(sc.all_microphones(include_loopback=False), spec)
        if mic is None:
            raise LookupError(f"no microphone matching {spec!r}")
        self.mic_spec = spec
        return mic.name

    def stop(self):
        """End the capture; returns the recorded float32 mono audio at
        16 kHz (empty array when nothing was captured)."""
        import numpy as np

        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)
        if not self._chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self._chunks)

    def toggle(self):
        """Push-to-talk: start when idle, stop when recording. Returns
        (now_recording, audio_or_None)."""
        if self.recording:
            return False, self.stop()
        self.start()
        return True, None

    def transcribe(self, audio):
        """Audio -> text with faster-whisper (lazy model load). Returns ''
        on silence, too-short audio, or any failure — callers treat ''
        as 'not heard'."""
        if audio is None or len(audio) < SAMPLE_RATE * MIN_SECONDS:
            return ""
        try:
            if self._transcriber is not None:
                return self._transcriber(audio).strip()
            segments, _info = self._load_model().transcribe(
                audio, beam_size=5, vad_filter=True
            )
            return " ".join(s.text.strip() for s in segments).strip()
        except Exception:  # noqa: BLE001 — hearing must never kill anything
            return ""

    def _capture(self, mic):
        try:
            with mic.recorder(samplerate=SAMPLE_RATE) as rec:
                while not self._stop.is_set():
                    self._chunks.append(_mono(rec.record(numframes=SAMPLE_RATE // 10)))
        except Exception:  # noqa: BLE001 — device died mid-capture: "nothing heard"
            self._chunks = []

    def _load_model(self):
        with self._model_lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                model, device, compute = _stt_config()
                self._model = WhisperModel(model, device=device, compute_type=compute)
            return self._model
