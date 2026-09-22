"""Unit tests for the slash-command registry helpers."""

from replicanta import speech, tui_commands


def test_filter_commands_matches_name_and_description():
    assert any(c[0] == "/chaos" for c in tui_commands.filter_commands("randomness"))
    assert any(c[0] == "/voice" for c in tui_commands.filter_commands("voice"))
    assert tui_commands.filter_commands("xyzxyz") == []


def test_palette_items_returns_all_commands():
    assert tui_commands.palette_items() == tui_commands.COMMANDS


def test_every_command_has_a_category():
    for command in tui_commands.COMMANDS:
        assert len(command) == 4
        assert command[3] in {"State", "Voice", "Senses", "MUD", "Organisms", "System", "Help"}


# -- one vocabulary source: registry == metadata == completion ---------------


def test_dispatch_registry_matches_command_metadata():
    """Dispatch, help, and tab completion share a single vocabulary: every
    COMMANDS name has a registry handler and no handler is undocumented."""
    names = {c[0] for c in tui_commands.COMMANDS}
    assert set(tui_commands.COMMAND_HANDLERS) == names
    assert all(callable(h) for h in tui_commands.COMMAND_HANDLERS.values())


def test_completion_derives_from_the_same_vocabulary():
    assert [c[0] for c in tui_commands.COMMANDS] == tui_commands.COMMAND_NAMES
    assert tui_commands.completion_matches("/") == tui_commands.COMMAND_NAMES


# -- tab completion -------------------------------------------------------------


def test_completion_matches_prefix():
    assert tui_commands.completion_matches("/c") == ["/chaos", "/camera"]
    assert tui_commands.completion_matches("/") == tui_commands.COMMAND_NAMES
    assert tui_commands.completion_matches("hello") is None
    assert tui_commands.completion_matches("") is None


def test_complete_command_cycles_fixed_candidate_list():
    """Consecutive Tabs over candidates captured for the typed token
    cycle through every match instead of collapsing to the first one."""
    matches = tui_commands.completion_matches("/c")
    value, index = "/c", 0
    seen = []
    for _ in range(4):
        value, index = tui_commands.complete_command(value, matches, index)
        seen.append(value)
    assert seen == ["/chaos", "/camera", "/chaos", "/camera"]


def test_complete_command_preserves_args_after_token():
    """Replacing the typed token keeps whatever was typed after it."""
    matches = tui_commands.completion_matches("/c")
    value, index = "/c 0.5", 0
    value, index = tui_commands.complete_command(value, matches, index)
    assert value == "/chaos 0.5"
    value, index = tui_commands.complete_command(value, matches, index)
    assert value == "/camera 0.5"


def test_complete_command_without_matches_is_noop():
    value, index = tui_commands.complete_command("/zzz", [], 0)
    assert value == "/zzz"
    assert index == 0


# -- /voice on: surface a broken TTS runtime instead of silently staying mute -


def test_voice_on_warns_when_voice_extra_missing(monkeypatch):
    """A venv recreated without the voice extras used to pass the model-file
    check and then silently no-op every say(); /voice on must call that out."""
    monkeypatch.setattr(speech, "ready", lambda: False)
    monkeypatch.setattr(speech, "available", lambda: True)
    said = []
    monkeypatch.setattr(speech, "say", lambda text: said.append(text))
    try:
        message, warn = tui_commands.voice_command(["on"])
        assert warn
        assert "voice" in message and "extra" in message
        assert said == []  # nothing to speak with
    finally:
        speech.set_enabled(False)


def test_voice_on_warns_when_no_model(monkeypatch):
    monkeypatch.setattr(speech, "ready", lambda: False)
    monkeypatch.setattr(speech, "available", lambda: False)
    monkeypatch.setattr(speech, "model_path", lambda: "/nope.onnx")
    monkeypatch.setattr(speech, "say", lambda text: None)
    try:
        message, warn = tui_commands.voice_command(["on"])
        assert warn
        assert "no piper model" in message
    finally:
        speech.set_enabled(False)


def test_voice_on_speaks_when_ready(monkeypatch):
    monkeypatch.setattr(speech, "ready", lambda: True)
    monkeypatch.setattr(speech, "available", lambda: True)
    said = []
    monkeypatch.setattr(speech, "say", lambda text: said.append(text))
    try:
        message, warn = tui_commands.voice_command(["on"])
        assert not warn
        assert "on" in message
        assert said == ["I can speak now."]
    finally:
        speech.set_enabled(False)
