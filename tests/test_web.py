"""Integration coverage for the buildless Glasshouse web interface."""

import json
import logging
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from replicanta import extensions, nursery, rdd
from replicanta.organism import Organism
from replicanta.web import Glasshouse, make_server

SEED = Path(__file__).parent.parent / "organism.scl"


def _fake_respond(org, text):
    reply = f"heard: {text}"
    org.store.record_chat("org", reply)
    return reply


@pytest.fixture
def glasshouse(tmp_path):
    shutil.copy(SEED, tmp_path / "organism.scl")
    org_dir = nursery.create(tmp_path, "default", tmp_path / "organism.scl")
    nursery.set_current(tmp_path, "default")
    org = Organism(org_dir)
    org.load()
    return Glasshouse(tmp_path, org, respond=_fake_respond)


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
        app = Glasshouse(tmp_path, org, respond=_fake_respond)
        server = make_server(app, port=0)
        shared.update(server=server, app=app)
        ready.set()
        server.serve_forever()
        server.server_close()
        app.org.mind.close()
        server.glasshouse = None
        shared.pop("app", None)
        org.mind.close()
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


_UNSET = object()


def request(base, path, data=None, token=_UNSET):
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json"}
    if token is _UNSET and hasattr(base, "app"):
        token = base.app.token
    if token is not None and token is not _UNSET:
        headers["X-Replicanta-Token"] = token
    req = urllib.request.Request(
        base + path,
        data=body,
        headers=headers,
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
        js = response.read()
        assert b"/api/" in js
        assert b"X-Replicanta-Token" in js


def test_state_is_real_organism_state(live):
    status, _headers, state = request(live, "/api/state")
    assert status == 200
    assert state["organism"]["name"] == "default"
    assert state["organism"]["state"] == "wake"
    assert state["nursery"]["organisms"] == ["default"]
    assert isinstance(state["beliefs"], list)


def test_mutating_endpoints_require_token(live):
    status, _headers, result = request(live, "/api/chat", {"text": "hello"}, token=None)
    assert status == 401
    assert result["error"] == "unauthorized"
    status, _headers, result = request(live, "/api/chat", {"text": "hello"}, token="wrong-token")
    assert status == 401
    assert result["error"] == "unauthorized"


def test_failed_auth_is_logged_without_token(live, caplog):
    """Rejected requests are logged (method, path, client IP) — but the
    presented credential never is: a typo'd real token must not land in logs."""
    with caplog.at_level(logging.WARNING, logger="replicanta.web"):
        status, _headers, _ = request(live, "/api/chat", {"text": "hello"}, token="wrong-token")
    assert status == 401
    messages = [r.getMessage() for r in caplog.records]
    assert any("rejected unauthorized POST /api/chat from 127.0.0.1" in m for m in messages)
    assert not any("wrong-token" in m for m in messages)


def test_state_requires_token(live):
    # /api/state carries chat history, memory, and camera frames — it must
    # not be readable without the bearer token.
    status, _headers, result = request(live, "/api/state", token=None)
    assert status == 401
    assert result["error"] == "unauthorized"
    status, _headers, result = request(live, "/api/state", token="wrong-token")
    assert status == 401
    status, _headers, result = request(live, "/api/state")
    assert status == 200
    # Command metadata stays public (no sensitive content).
    status, _headers, result = request(live, "/api/commands", token=None)
    assert status == 200


def test_host_header_mismatch_is_rejected(live):
    # DNS-rebinding guard: a page on attacker.example rebinds to 127.0.0.1
    # but keeps its own Host header.
    for method_path in ["/", "/api/state"]:
        req = urllib.request.Request(live + method_path, headers={"Host": "attacker.example"})
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req)
        assert caught.value.code == 403
        with caught.value:
            assert json.load(caught.value)["error"] == "forbidden host"


def test_export_is_confined_to_exports_dir(live, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    status, _headers, result = request(live, "/api/command", {"command": "/export notes"})
    assert status == 200
    dest = tmp_path / ".replicanta" / "exports" / "notes.md"
    assert dest.exists()
    assert any(str(dest) in m for m in result["messages"])

    status, _headers, result = request(live, "/api/command", {"command": "/export ../../.bashrc"})
    assert status == 200
    assert any("plain filename" in m for m in result["messages"])
    assert not (tmp_path / ".bashrc").exists()
    assert not (tmp_path / ".replicanta" / "exports" / ".bashrc").exists()


def test_visualize_endpoint_renders_chart(live):
    status, _headers, result = request(live, "/api/visualize", {"kind": "beliefs"})
    assert status == 200
    assert result["kind"] == "beliefs"
    assert "text_chart" in result
    assert "chart_path" in result
    assert result["chart_path"].endswith("beliefs-chart.svg")
    assert Path(result["chart_path"]).exists()
    assert result["state"]["organism"]["name"] == "default"

    status2, _headers2, result2 = request(live, "/api/visualize", {})
    assert status2 == 200
    assert result2["kind"] in rdd.supported_kinds()


def test_state_includes_persona_snapshot(live):
    status, _headers, state = request(live, "/api/state")
    assert status == 200
    assert "persona" in state
    assert isinstance(state["persona"]["available"], list)
    assert "active" in state["persona"]


def test_chat_runs_hear_reply_persist_pipeline(live):
    status, _headers, result = request(live, "/api/chat", {"text": "my name is sam"})
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
    status, _headers, result = request(live, "/api/lifecycle", {"action": "sleep"})
    assert status == 200
    assert result["state"]["organism"]["state"] == "sleep"
    status, _headers, result = request(live, "/api/lifecycle", {"action": "wake"})
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
    status, _headers, result = request(live, "/api/mutation", {"action": "approve"})
    assert status == 200
    assert result["entry"] == entry
    assert result["state"]["extensions"]["applied"] == [entry]
    status, _headers, result = request(live, "/api/mutation", {"action": "revert"})
    assert status == 200
    assert result["entry"] == entry
    extensions.propose(live.app.extension_path, entry)
    status, _headers, result = request(live, "/api/mutation", {"action": "reject"})
    assert status == 200
    assert result["state"]["extensions"]["pending"] is None


def test_nursery_create_and_swap(live):
    status, _headers, state = request(live, "/api/organisms", {"name": "fern"})
    assert status == 200
    assert state["organism"]["name"] == "fern"
    assert state["nursery"]["organisms"] == ["default", "fern"]
    status, _headers, state = request(live, "/api/swap", {"name": "default"})
    assert status == 200
    assert state["organism"]["name"] == "default"


def test_bad_routes_and_malformed_json_are_safe(live):
    status, _headers, result = request(live, "/api/missing")
    assert status == 404
    assert result == {"error": "not found"}
    req = urllib.request.Request(
        live + "/api/chat",
        data=b"{broken",
        headers={
            "Content-Type": "application/json",
            "X-Replicanta-Token": live.app.token,
        },
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req)
    assert caught.value.code == 400
    with caught.value:
        assert json.load(caught.value)["error"] == "invalid JSON"


def test_server_uses_http_server(glasshouse):
    from http.server import HTTPServer

    server = make_server(glasshouse, port=0)
    try:
        assert isinstance(server, HTTPServer)
    finally:
        server.server_close()


def test_server_binds_loopback_by_default(glasshouse):
    server = make_server(glasshouse, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_commands_endpoint_returns_metadata(live):
    status, _headers, result = request(live, "/api/commands")
    assert status == 200
    assert isinstance(result, list)
    assert any(cmd["name"] == "/sleep" for cmd in result)
    assert all("name" in cmd and "usage" in cmd and "description" in cmd for cmd in result)


def test_command_runs_stats_and_help(live):
    status, _headers, result = request(live, "/api/command", {"command": "/stats"})
    assert status == 200
    assert result["state"]["organism"]["name"] == "default"
    assert any(msg.startswith("stats:") for msg in result["messages"])

    status, _headers, result = request(live, "/api/command", {"command": "/help"})
    assert status == 200
    assert "REPLICANTA" in result["messages"][0]


def test_state_requests_serialize_on_single_thread_server(live):
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    token = live.app.token
    req1 = urllib.request.Request(f"{live}/api/state", headers={"X-Replicanta-Token": token})
    req2 = urllib.request.Request(f"{live}/api/state", headers={"X-Replicanta-Token": token})
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(urllib.request.urlopen, req1)
        f2 = pool.submit(urllib.request.urlopen, req2)
    # With HTTPServer requests are serialized, but both still succeed.
    assert f1.result().status == 200
    assert f2.result().status == 200


def test_command_sleep_and_wake(live):
    status, _headers, result = request(live, "/api/command", {"command": "/sleep"})
    assert status == 200
    assert result["state"]["organism"]["state"] == "sleep"
    status, _headers, result = request(live, "/api/command", {"command": "/wake"})
    assert status == 200
    assert result["state"]["organism"]["state"] == "wake"


def test_command_chaos_and_focus(live):
    status, _headers, result = request(live, "/api/command", {"command": "/chaos 0.42"})
    assert status == 200
    assert result["state"]["organism"]["chaos"] == 0.42

    status, _headers, result = request(live, "/api/command", {"command": "/focus mood"})
    assert status == 200
    assert any(pair[0] == "mood" for pair in result["state"]["attention"])

    status, _headers, error = request(live, "/api/command", {"command": "/chaos 2"})
    assert status == 400
    assert "between 0 and 1" in error["error"]


def test_typing_records_activity(live):
    before = live.app.org.store.activity.get("user_typing", 0)
    status, _headers, result = request(live, "/api/typing", {"typing": True})
    assert status == 200
    assert result["events"][0]["kind"] == "typing"
    assert live.app.org.store.activity.get("user_typing") == before + 1
    assert "typing_sessions" in live.app.org.store.activity


def test_command_lists_organisms_and_rejects_unknown(live):
    status, _headers, result = request(live, "/api/command", {"command": "/organisms"})
    assert status == 200
    assert "default" in result["messages"][0]

    status, _headers, error = request(live, "/api/command", {"command": "/unknown"})
    assert status == 400
    assert "unknown command" in error["error"]


def test_mud_start_creates_game(live):
    status, _headers, result = request(live, "/api/command", {"command": "/mud start"})
    assert status == 200
    assert result["state"]["mud"]["active"] is True
    assert result["state"]["mud"]["scenario"] == "The Amulet of Vatox"
    assert any(a["name"] == "default" for a in result["state"]["mud"]["roster"])


def test_mud_act_applies_command_and_advances_turn(live):
    request(live, "/api/command", {"command": "/mud start"})
    status, _headers, result = request(live, "/api/mud-act", {"text": "go north"})
    assert status == 200
    state = result["state"]
    assert state["mud"]["active"] is True
    default_actor = next(a for a in state["mud"]["roster"] if a["name"] == "default")
    assert default_actor["room"] == "cave mouth"


def test_mud_join_adds_second_organism(live):
    request(live, "/api/command", {"command": "/mud start"})
    request(live, "/api/organisms", {"name": "fern"})
    request(live, "/api/swap", {"name": "fern"})
    status, _headers, result = request(live, "/api/command", {"command": "/mud join default"})
    assert status == 200
    state = result["state"]
    assert state["mud"]["active"] is True
    names = {a["name"] for a in state["mud"]["roster"]}
    assert names == {"default", "fern"}


def test_settings_voice_and_git(live):
    from replicanta import speech

    try:
        status, _headers, state = request(live, "/api/settings", {"voice": "on"})
        assert status == 200
        assert state["speech"]["enabled"] is True

        status, _headers, state = request(live, "/api/settings", {"voice": "off"})
        assert status == 200
        assert state["speech"]["enabled"] is False

        status, _headers, state = request(live, "/api/settings", {"git": "on"})
        assert status == 200
        assert state["git_enabled"] is True

        status, _headers, state = request(live, "/api/settings", {"git": "off"})
        assert status == 200
        assert state["git_enabled"] is False
    finally:
        speech.set_enabled(False)


def test_swap_closes_previous_organism_mind(glasshouse, tmp_path, monkeypatch):
    """web.py must release the swapped-out organism's thread-affine Scallop
    context instead of leaving it for the cyclic GC to drop on a random thread."""
    from replicanta import organism as organism_module

    nursery.create(tmp_path, "fern", SEED)
    old_mind = glasshouse.org.mind
    closed = []
    monkeypatch.setattr(organism_module.Mind, "close", lambda self: closed.append(self))
    glasshouse.swap("fern")
    assert closed == [old_mind]


def test_release_mud_org_closes_throwaway_but_not_live(glasshouse, tmp_path, monkeypatch):
    """Throwaway MUD organisms get their minds closed; the live organism and
    None are left alone."""
    from replicanta import organism as organism_module

    nursery.create(tmp_path, "fern", SEED)
    throwaway = glasshouse._mud_organism_for("fern")
    assert throwaway is not None and throwaway is not glasshouse.org
    closed = []
    monkeypatch.setattr(organism_module.Mind, "close", lambda self: closed.append(self))
    glasshouse._release_mud_org(throwaway)
    assert closed == [throwaway.mind]
    glasshouse._release_mud_org(glasshouse.org)
    glasshouse._release_mud_org(None)
    assert closed == [throwaway.mind]
