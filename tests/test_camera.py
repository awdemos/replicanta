"""Sight feature: USB camera enumeration/matching, frame grabbing
(contained failures), vision-model description, organism.see(), and the
TUI /look + /camera wiring. No real camera or vision model is touched."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import camera
import narration
from camera import Camera


def _sysfs(tmp_path, devices):
    """Build a fake /sys/class/video4linux tree."""
    root = tmp_path / "video4linux"
    for index, name in devices:
        d = root / f"video{index}"
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
    return root


# -- enumeration + matching -----------------------------------------------------

def test_list_cameras_from_sysfs(tmp_path):
    root = _sysfs(tmp_path, [(0, "HD Webcam"), (2, "USB Camera 2")])
    (root / "not-a-device").mkdir()          # ignored
    assert camera.list_cameras(sysfs=root) == [
        (0, "HD Webcam"), (2, "USB Camera 2")]


def test_list_cameras_without_sysfs(tmp_path):
    assert camera.list_cameras(sysfs=tmp_path / "missing") == []


def test_match_camera_by_index_path_and_name():
    cams = [(0, "HD Webcam"), (2, "USB Camera 2")]
    assert camera.match_camera(cams, "2") == (2, "USB Camera 2")
    assert camera.match_camera(cams, "/dev/video0") == (0, "HD Webcam")
    assert camera.match_camera(cams, "webcam") == (0, "HD Webcam")
    assert camera.match_camera(cams, "nope") is None


def test_set_device_validates():
    cam = Camera()
    monkey_cams = [(3, "Obsbot Tiny")]
    orig = camera.list_cameras
    camera.list_cameras = lambda: monkey_cams
    try:
        assert cam.set_device("obsbot") == (3, "Obsbot Tiny")
        assert cam.device == 3
        with pytest.raises(LookupError):
            cam.set_device("webcam")
    finally:
        camera.list_cameras = orig


# -- grabbing ----------------------------------------------------------------

def test_grab_uses_injected_grabber():
    cam = Camera(grabber=lambda: b"\xff\xd8jpeg")
    assert cam.grab() == b"\xff\xd8jpeg"


def test_grab_no_camera_returns_none():
    # real path with no camera attached: cv2 fails or the device won't
    # open — either way None, never an exception
    cam = Camera()
    cam.device = 999
    assert cam.grab() is None


# -- vision model --------------------------------------------------------------

def test_describe_image_posts_base64(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            import json as j
            return j.dumps({"response": "a desk with a lamp"}).encode()

    def fake_urlopen(req, timeout):
        import json as j
        seen["payload"] = j.loads(req.data.decode())
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(narration.urllib.request, "urlopen", fake_urlopen)
    out = narration.describe_image(b"\xff\xd8fake-jpeg")
    assert out == "a desk with a lamp"
    assert seen["payload"]["images"] == ["/9hmYWtlLWpwZWc="]
    assert seen["payload"]["model"] == narration.VISION_MODEL


def test_describe_image_raises_on_ollama_error(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            import json as j
            return j.dumps({"error": "model not found"}).encode()

    monkeypatch.setattr(
        narration.urllib.request, "urlopen", lambda req, timeout: _Resp())
    with pytest.raises(RuntimeError):
        narration.describe_image(b"x")


# -- organism + prompt ----------------------------------------------------------

def test_see_remembers_episode_and_keeps_last_sight(tmp_path):
    from organism import Organism
    org = Organism(tmp_path)
    org.load()
    org.see("a cat on the keyboard")
    assert org.last_sight == "a cat on the keyboard"
    assert any("cat" in m["text"] for m in org.store.memory)


def test_snapshot_and_felt_experience_include_sight(tmp_path):
    from organism import Organism
    org = Organism(tmp_path)
    org.load()
    org.see("a window full of rain")
    snap = narration.state_snapshot(org)
    assert snap["sight"] == "a window full of rain"
    assert any("rain" in line for line in narration._felt_experience(snap))


def test_felt_experience_without_sight(tmp_path):
    from organism import Organism
    org = Organism(tmp_path)
    org.load()
    snap = narration.state_snapshot(org)
    assert snap["sight"] is None
    assert not any(line.startswith("sight:")
                   for line in narration._felt_experience(snap))


# -- TUI wiring ---------------------------------------------------------------

def test_camera_command_status_and_use(tmp_path):
    from organism import Organism
    from tui import OrganismApp
    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    app.camera = Camera(grabber=lambda: b"\xff\xd8jpeg")
    app.handle_command("/camera")              # status line, no crash
    monkey_cams = [(1, "HD Webcam")]
    orig = camera.list_cameras
    camera.list_cameras = lambda: monkey_cams
    try:
        app.handle_command("/camera use webcam")
        assert app.camera.device == 1
    finally:
        camera.list_cameras = orig


def test_set_sight_feeds_the_organism(tmp_path):
    from organism import Organism
    from tui import OrganismApp
    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    app._set_sight("a houseplant by the monitor")
    assert org.last_sight == "a houseplant by the monitor"
