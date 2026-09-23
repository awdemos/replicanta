"""Integration coverage for the buildless Glasshouse web interface."""

import http.client
import json
import logging
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from replicanta import extensions, nursery, rdd
from replicanta.organism import Organism
from replicanta.web import Glasshouse, make_server
from replicanta.web_static import APP_JS

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


def test_non_public_api_get_requires_token(live):
    # do_GET must consult PUBLIC_API_GETS: any other /api/* GET is
    # default-deny, mirroring the POST side — unknown routes leak nothing
    # about the API surface to unauthenticated clients.
    assert Glasshouse.PUBLIC_API_GETS == ("/api/commands",)
    status, _headers, result = request(live, "/api/nope", token=None)
    assert status == 401
    assert result["error"] == "unauthorized"
    status, _headers, result = request(live, "/api/nope", token="wrong-token")
    assert status == 401
    # Authenticated but still unknown: the normal not-found path.
    status, _headers, result = request(live, "/api/nope")
    assert status == 404
    assert result["error"] == "not found"
    # The route named by the policy stays public.
    status, _headers, _result = request(live, "/api/commands", token=None)
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
    assert any("— awake · mood:" in msg for msg in result["messages"])
    assert any(msg.startswith("mind:") for msg in result["messages"])

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


def test_non_object_json_body_is_rejected(live):
    """A syntactically valid JSON body that isn't an object must be a clean
    400; route lambdas call data.get(...) and a list/str/number/null body used
    to raise AttributeError and reset the connection."""
    for raw in (b"[1,2,3]", b'"hello"', b"42", b"null"):
        req = urllib.request.Request(
            live + "/api/chat",
            data=raw,
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
            assert "invalid JSON body" in json.load(caught.value)["error"]
    # The rejections were answered, not connection resets — server is healthy.
    status, _headers, _state = request(live, "/api/state")
    assert status == 200


def test_non_ascii_auth_token_is_rejected_cleanly(live):
    """secrets.compare_digest raises TypeError on non-ASCII str tokens, which
    escaped auth_ok and dropped the connection. A weird credential must fail
    closed with a 401 response."""
    port = urlsplit(live).port
    for header, value in (
        ("Authorization", "Bearer tökén"),
        ("X-Replicanta-Token", "tökén"),
    ):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/api/chat",
            body=b"{}",
            headers={"Content-Type": "application/json", header: value},
        )
        response = conn.getresponse()
        payload = json.loads(response.read())
        conn.close()
        assert response.status == 401
        assert payload == {"error": "unauthorized"}
    # The server is still serving after the odd headers.
    status, _headers, _state = request(live, "/api/state")
    assert status == 200


def test_mud_log_is_escaped_before_rendering(live):
    """Stored MUD commands flow into state.mud.log verbatim; the client must
    map every line through esc() before writing innerHTML, or a command like
    `take <b>injected</b>` injects markup into the page."""
    request(live, "/api/command", {"command": "/mud start"})
    status, _headers, result = request(live, "/api/mud-act", {"text": "take <b>injected</b>"})
    assert status == 200
    log = result["state"]["mud"]["log"]
    assert any("<b>injected</b>" in line for line in log)
    assert "(s.mud.log||[]).map(esc).join(" in APP_JS
    assert "(s.mud.log||[]).join(" not in APP_JS


def test_typing_throttle_checks_elapsed_time_before_refreshing(live):
    """debounceTyping must compare now against the previous lastTyping before
    assigning lastTyping=now; assigning first made the ~4s throttle dead code
    and POSTed /api/typing on every keystroke."""
    start = APP_JS.index("function debounceTyping")
    end = APP_JS.index("const chatArea", start)
    body = APP_JS[start:end]
    assert body.index("now-lastTyping>4000") < body.index("lastTyping=now")


def test_mud_reset_requires_host(live):
    request(live, "/api/command", {"command": "/mud start"})
    request(live, "/api/organisms", {"name": "fern"})
    request(live, "/api/swap", {"name": "fern"})
    status, _headers, result = request(live, "/api/command", {"command": "/mud join default"})
    assert status == 200
    game = live.app._mud_games["default"]

    status, _headers, result = request(live, "/api/command", {"command": "/mud reset"})
    assert status == 200
    assert "only the host" in result["messages"][0]
    # A member's reset attempt must leave the host's game untouched.
    assert live.app._mud_games["default"] is game
    assert live.app._mud_member_of["fern"] == "default"


def test_mud_reset_restarts_host_game(live):
    request(live, "/api/command", {"command": "/mud start"})
    status, _headers, result = request(live, "/api/command", {"command": "/mud reset"})
    assert status == 200
    assert "entered" in result["messages"][0]
    assert result["state"]["mud"]["active"] is True
    assert "default" in live.app._mud_games


def test_mud_start_refused_while_member_of_another_game(live):
    request(live, "/api/command", {"command": "/mud start"})
    request(live, "/api/organisms", {"name": "fern"})
    request(live, "/api/swap", {"name": "fern"})
    status, _headers, result = request(live, "/api/command", {"command": "/mud join default"})
    assert status == 200

    # Fern is still seated in default's game, so it cannot host a new one.
    status, _headers, result = request(live, "/api/command", {"command": "/mud start"})
    assert status == 200
    assert "already in a game" in result["messages"][0]
    assert set(live.app._mud_games) == {"default"}
    assert live.app._mud_member_of["fern"] == "default"

    # After leaving the old seat, hosting works.
    request(live, "/api/command", {"command": "/mud leave"})
    status, _headers, result = request(live, "/api/command", {"command": "/mud start"})
    assert status == 200
    assert "entered" in result["messages"][0]
    assert "fern" in live.app._mud_games
    assert live.app._mud_member_of["fern"] == "fern"


def test_state_payload_has_metric_tables(live):
    """The Inner tab tables ride the state payload: structured activity
    counters with per-cycle rates plus a mind-metrics summary, with the
    prose `activity` field kept for backward compatibility. Only counters
    with nonzero totals appear — no wall of zeros."""
    status, _headers, state = request(live, "/api/state")
    assert status == 200
    table = state["activity_table"]
    assert set(table) <= {
        "rules_tried",
        "derivations",
        "beliefs_new",
        "beliefs_strengthened",
        "beliefs_archived",
        "rules_committed",
        "dreams_promoted",
        "dreams_discarded",
        "llm_calls",
        "prompt_tokens",
        "gen_tokens",
        "utterances",
        "fallbacks",
        "facts_learned",
        "grounded_utterances",
    }
    assert all(set(row) == {"total", "per_cycle"} for row in table.values())
    # Zero-activity counters stay out of the table.
    assert all(
        state["metrics"]["activity"][key] == 0
        for key in set(state["metrics"]["activity"]) - set(table)
        if isinstance(state["metrics"]["activity"][key], int)
    )
    mm = state["mind_metrics"]
    assert set(mm) == {
        "beliefs",
        "rules",
        "memories",
        "cycle",
        "score",
        "goals_active",
        "goals_done",
    }
    assert mm["beliefs"] == len(state["beliefs"])
    assert mm["goals_active"] + mm["goals_done"] == len(state["goals"])
    assert mm["cycle"] == state["organism"]["cycle"]
    assert mm["score"] >= 0
    assert isinstance(state["activity"], list)

    # Once something happens, nonzero counters appear with totals + rates.
    request(live, "/api/chat", {"text": "my name is sam"})
    status, _headers, state = request(live, "/api/state")
    assert status == 200
    table = state["activity_table"]
    assert table, "chat should bump at least one activity counter"
    cycle = max(state["organism"]["cycle"], 1)
    for counter, row in table.items():
        assert set(row) == {"total", "per_cycle"}
        assert row["total"] > 0
        assert row["per_cycle"] == round(row["total"] / cycle, 2)
        assert state["metrics"]["activity"][counter] == row["total"]


def test_inner_tab_renders_metric_tables_from_state(live):
    """The Inner tab builds both tables from the structured payload fields,
    escaping every interpolation — not from the prose activity lines."""
    assert "s.mind_metrics" in APP_JS
    assert "s.activity_table" in APP_JS
    assert 'class="metrics"' in APP_JS
    assert "esc(at[k].total)" in APP_JS
    assert "esc(r[1])" in APP_JS


# -- background scheduler + load_error banner --------------------------------


def test_scheduler_ticks_the_organism_between_requests(glasshouse):
    """Web-hosted organisms must live between requests: the daemon scheduler
    advances fatigue/stress/mood exactly like the TUI's 1s tick."""
    import time as _time

    glasshouse.start_scheduler(interval=0.05)
    try:
        fatigue = glasshouse.org.store.fatigue
        deadline = _time.monotonic() + 5
        while glasshouse.org.store.fatigue == fatigue and _time.monotonic() < deadline:
            _time.sleep(0.02)
        assert glasshouse.org.store.fatigue > fatigue  # wake accrual ran
    finally:
        glasshouse.stop_scheduler()


def test_scheduler_reentry_guard(glasshouse):
    glasshouse.start_scheduler(interval=0.05)
    try:
        assert glasshouse._scheduler is not None
        # a second start is a no-op: same thread object
        scheduler = glasshouse._scheduler
        glasshouse.start_scheduler()
        assert glasshouse._scheduler is scheduler
    finally:
        glasshouse.stop_scheduler()
    assert glasshouse._scheduler is None


def test_load_error_banner_renders_once_on_the_shell(live):
    live.app.org.store.load_error = "state.json was corrupt (boom); preserved as state.json.corrupt-x"
    with urllib.request.urlopen(live + "/") as response:
        html = response.read().decode()
    assert "Recovered from a damaged save" in html
    assert "state.json was corrupt" in html
    # cleared after first render: the next GET is quiet
    assert live.app.org.store.load_error is None
    with urllib.request.urlopen(live + "/") as response:
        html = response.read().decode()
    assert "Recovered from a damaged save" not in html


def test_load_error_appears_once_in_state_payload(live):
    live.app.org.store.load_error = "organism.scl was quarantined"
    status, _headers, state = request(live, "/api/state")
    assert status == 200
    assert state["load_error"] == "organism.scl was quarantined"
    assert live.app.org.store.load_error is None
    _status, _headers, state = request(live, "/api/state")
    assert "load_error" not in state
