"""Sight: USB camera frames -> JPEG bytes.

Optional like speech.py and listen.py: cv2 is imported lazily, every
failure is contained (no camera, busy device or missing package just
means no sight, never a crash). Capture goes through opencv's V4L2
backend; device names come from /sys/class/video4linux (no v4l2-ctl
needed). The TUI (/look) grabs a frame, narration turns it into words
with a local vision model, and the organism remembers what it saw.
"""

import re
from pathlib import Path

WIDTH, HEIGHT = 640, 480
WARMUP_FRAMES = 5        # webcams need a few frames for auto-exposure
JPEG_QUALITY = 85
SYSFS = Path("/sys/class/video4linux")

_VIDEO_RE = re.compile(r"^video(\d+)$")


def list_cameras(sysfs=SYSFS):
    """[(index, name)] of V4L2 devices, index being the N in /dev/videoN.
    Names come from sysfs; [] when no camera is plugged in."""
    cams = []
    if not sysfs.is_dir():
        return cams
    for entry in sorted(sysfs.iterdir()):
        m = _VIDEO_RE.match(entry.name)
        if not m:
            continue
        try:
            name = (entry / "name").read_text().strip()
        except OSError:
            name = entry.name
        cams.append((int(m.group(1)), name))
    return cams


def match_camera(cams, spec):
    """The camera whose index, /dev path, or name substring matches spec;
    None when nothing matches."""
    if spec.isdigit():
        for index, name in cams:
            if index == int(spec):
                return (index, name)
    m = _VIDEO_RE.match(Path(spec).name)
    if m:
        for index, name in cams:
            if index == int(m.group(1)):
                return (index, name)
    needle = spec.lower()
    for cam in cams:
        if needle in cam[1].lower():
            return cam
    return None


class Camera:
    """One frame at a time from a USB camera. `grabber` is injectable for
    tests; the real path opens the device, discards warmup frames, and
    JPEG-encodes one frame. grab() never raises; set_device() raises
    LookupError when no camera matches."""

    def __init__(self, grabber=None):
        self._grabber = grabber
        self.device = 0      # opencv device index (the N in /dev/videoN)

    def grab(self):
        """One JPEG frame (bytes), or None when no frame could be taken."""
        if self._grabber is not None:
            return self._grabber()
        try:
            import cv2
            cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
            try:
                if not cap.isOpened():
                    return None
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
                ok, frame = False, None
                for _ in range(WARMUP_FRAMES):
                    ok, frame = cap.read()
                if not ok or frame is None:
                    return None
                ok, buf = cv2.imencode(
                    ".jpg", frame,
                    [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                return buf.tobytes() if ok else None
            finally:
                cap.release()
        except Exception:  # noqa: BLE001 — sight must never kill anything
            return None

    def set_device(self, spec):
        """Choose the camera for future grabs (index, /dev/videoN, or name
        substring). Returns the matched (index, name); raises LookupError
        when nothing matches."""
        cam = match_camera(list_cameras(), spec)
        if cam is None:
            raise LookupError(f"no camera matching {spec!r}")
        self.device = cam[0]
        return cam
