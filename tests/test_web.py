"""Integration coverage for the buildless Glasshouse web interface."""

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from replicanta import extensions, nursery
from replicanta.organism import Organism
from replicanta.web import Glasshouse, make_server

SEED = Path(__file__).parent.parent / "organism.scl"


@pytest.fixture
def glasshouse(tmp_path):
    shutil.copy(SEED, tmp_path / "organism.scl")
    org_dir = nursery.create(tmp_path, "default", tmp_path / "organism.scl")
    nursery.set_current(tmp_path, "default")
    org = Organism(org_dir)
    org.load()
    return Glasshouse(tmp_path, org, respond=lambda _org, text: f"heard: {text}")


@pytest.fixture
def live(tmp_path):
    ready = threading.Event()
    shared = {}

    def serve():
        shutil.copy(SEED, tmp_path / "organism.scl")
        org_dir = nursery.create(tmp_path, "default", tmp_path / "organism.scl")
        nursery.set_current(tmp_path, "default")
        org = Organism(org_dir)
        org.load()
        app = Glasshouse(
            tmp_path, org, respond=lambda _org, text: f"heard: {text}"
        )
        server = make_server(app, port=0)
        shared.update(server=server, app=app)
        ready.set()
        server.serve_forever()
        server.server_close()
        app.org.mind.ctx = None
        server.glasshouse = None
        shared.pop("app", None)
        org.mind.ctx = None
        del app, org

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(5)

    class LiveURL(str):
        pass

    url = LiveURL(f"http://127.0.0.1:{shared['server'].server_port}")
    url.app = shared["app"]
    yield url
    del url.app
    shared["server"].shutdown()
    thread.join(timeout=2)
    shared.clear()


def request(base, path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        base + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, response.headers, json.load(response)
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.headers, json.load(exc)


def test_web_shell_and_assets_are_served(live):
    with urllib.request.urlopen(live + "/") as response:
        html = response.read().decode()
        assert response.status == 200
        assert "Replicanta Glasshouse" in html
        assert "app.js" in html
        assert "Content-Security-Policy" in response.headers
    with urllib.request.urlopen(live + "/app.css") as response:
        assert b".organism" not in response.read()  # CSS, not demo JSON
    with urllib.request.urlopen(live + "/app.js") as response:
        assert b"/api/" in response.read()


def test_state_is_real_organism_state(live):
    status, _headers, state = request(live, "/api/state")
    assert status == 200
    assert state["organism"]["name"] == "default"
    assert state["organism"]["state"] == "wake"
    assert state["nursery"]["organisms"] == ["default"]
    assert isinstance(state["beliefs"], list)


def test_chat_runs_hear_reply_persist_pipeline(live):
    status, _headers, result = request(
        live, "/api/chat", {"text": "my name is sam"}
    )
    assert status == 200
    assert result["reply"] == "heard: my name is sam"
    assert any(event["kind"] == "learned" for event in result["events"])
    assert result["state"]["chat"][-2:] == [
        {"role": "user", "text": "my name is sam"},
        {"role": "org", "text": "heard: my name is sam"},
    ]
    assert live.app.org.store.state_path.exists()


@pytest.mark.parametrize("text", ["", "x" * 4001])
def test_chat_rejects_empty_or_oversize_input(live, text):
    status, _headers, result = request(live, "/api/chat", {"text": text})
    assert status == 400
    assert "1-4000" in result["error"]


def test_lifecycle_sleep_and_wake(live):
    status, _headers, result = request(
        live, "/api/lifecycle", {"action": "sleep"}
    )
    assert status == 200
    assert result["state"]["organism"]["state"] == "sleep"
    status, _headers, result = request(
        live, "/api/lifecycle", {"action": "wake"}
    )
    assert status == 200
    assert result["state"]["organism"]["state"] == "wake"


def test_settings_cover_chaos_focus_and_mutation_consent(live):
    status, _headers, state = request(
        live,
        "/api/settings",
        {"chaos": 0.8, "focus": "mood", "auto_apply": False},
    )
    assert status == 200
    assert state["organism"]["chaos"] == 0.8
    assert state["organism"]["auto_apply"] is False
    assert any(pair[0] == "mood" for pair in state["attention"])
    status, _headers, error = request(live, "/api/settings", {"chaos": 2})
    assert status == 400
    assert "between 0 and 1" in error["error"]


def test_mutation_approve_reject_and_revert(live):
    entry = {"kind": "seed", "text": "consider the source"}
    extensions.propose(live.app.extension_path, entry)
    status, _headers, result = request(
        live, "/api/mutation", {"action": "approve"}
    )
    assert status == 200
    assert result["entry"] == entry
    assert result["state"]["extensions"]["applied"] == [entry]
    status, _headers, result = request(
        live, "/api/mutation", {"action": "revert"}
    )
    assert status == 200
    assert result["entry"] == entry
    extensions.propose(live.app.extension_path, entry)
    status, _headers, result = request(
        live, "/api/mutation", {"action": "reject"}
    )
    assert status == 200
    assert result["state"]["extensions"]["pending"] is None


def test_nursery_create_and_swap(live):
    status, _headers, state = request(
        live, "/api/organisms", {"name": "fern"}
    )
    assert status == 200
    assert state["organism"]["name"] == "fern"
    assert state["nursery"]["organisms"] == ["default", "fern"]
    status, _headers, state = request(live, "/api/swap", {"name": "default"})
    assert status == 200
    assert state["organism"]["name"] == "default"


def test_rename_current_organism(live):
    status, _headers, state = request(live, "/api/rename", {"name": "moss"})
    assert status == 200
    assert state["organism"]["name"] == "moss"
    assert state["nursery"]["current"] == "moss"
    assert "moss" in state["nursery"]["organisms"]


def test_cycle_advances_state(live):
    status, _headers, result = request(live, "/api/cycle", {})
    assert status == 200
    assert result["state"]["organism"]["cycle"] >= 0


def test_remember_records_episode(live):
    status, _headers, result = request(
        live, "/api/remember", {"text": "met in the garden"}
    )
    assert status == 200
    assert any(
        e["kind"] == "user" and "garden" in e["text"]
        for e in result["state"]["memory"]
    )


def test_forget_removes_matching_entries(live):
    request(live, "/api/remember", {"text": "met in the garden"})
    status, _headers, result = request(live, "/api/forget", {"text": "garden"})
    assert status == 200
    assert not any(
        "garden" in e.get("text", "") for e in result["state"]["memory"]
    )


def test_goal_and_priority(live):
    status, _headers, result = request(live, "/api/goal", {"text": "learn names"})
    assert status == 200
    assert any(
        "learn names" in str(g.get("text", "")) for g in result["state"]["goals"]
    )
    status, _headers, result = request(live, "/api/priority", {"goal": "names"})
    assert status == 200
    assert "names" in result["state"]["goals"][0].get("text", "")


def test_attention_focuses_window(live):
    request(live, "/api/chat", {"text": "my name is sam"})
    status, _headers, result = request(live, "/api/attention", {"topic": "sam"})
    assert status == 200
    assert result["state"]["attention"]


def test_mode_lifecycle(live):
    status, _headers, result = request(live, "/api/mode", {"mode": "sleep"})
    assert status == 200
    assert result["state"]["organism"]["state"] == "sleep"
    status, _headers, result = request(live, "/api/mode", {"mode": "wake"})
    assert status == 200
    assert result["state"]["organism"]["state"] == "wake"


def test_save_load_and_reset(live):
    request(live, "/api/remember", {"text": "token to persist"})
    status, _headers, result = request(live, "/api/save", {})
    assert status == 200
    assert result["state"]["organism"]["name"] == "default"
    status, _headers, state = request(live, "/api/reset", {})
    assert status == 200
    assert not any(
        "token to persist" in e.get("text", "")
        for e in state["memory"]
    )
    status, _headers, result = request(live, "/api/load", {})
    assert status == 200
    assert result["state"]["organism"]["name"] == "default"


def test_mutate_stages_seed(live):
    status, _headers, result = request(
        live, "/api/mutate", {"text": "consider kindness"}
    )
    assert status == 200
    assert result["state"]["extensions"]["pending"]["kind"] == "seed"
    status, _headers, result = request(live, "/api/mutation", {"action": "approve"})
    assert result["state"]["extensions"]["pending"] is None


def test_help_lists_slash_commands(live):
    status, _headers, result = request(live, "/api/help", {})
    assert status == 200
    names = {c["name"] for c in result["commands"]}
    assert "/help" in names
    assert "/mutate" in names
    assert result["state"]["organism"]["name"] == "default"


def test_bad_routes_and_malformed_json_are_safe(live):
    status, _headers, result = request(live, "/api/missing")
    assert status == 404
    assert result == {"error": "not found"}
    req = urllib.request.Request(
        live + "/api/chat",
        data=b"{broken",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req)
    assert caught.value.code == 400
    with caught.value:
        assert json.load(caught.value)["error"] == "invalid JSON"


def test_server_binds_loopback_by_default(glasshouse):
    server = make_server(glasshouse, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()
