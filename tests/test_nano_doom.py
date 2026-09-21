"""Service- and module-level tests for the nano-doom capability bridge."""

import pytest

from replicanta import nano_doom
from replicanta.modules import ModuleLoader


@pytest.fixture
def service():
    return nano_doom.DoomService()


def _load_nano_doom(tmp_path, logs):
    import shutil
    from pathlib import Path

    src = Path(__file__).parent.parent / "modules"
    shutil.copytree(src, tmp_path / "modules", dirs_exist_ok=True)
    loader = ModuleLoader(
        tmp_path / "modules",
        organism=None,
        modules_config={"enabled": ["base", "nano-doom"]},
        emit=logs.append,
    )
    loader.load_all()
    return loader


def test_service_available(service):
    assert service.available() is True


def test_start_default_map(service):
    out = service.start("default")
    assert "#" in out
    assert service.running() is True


def test_start_unknown_map_raises(service):
    with pytest.raises(ValueError, match="unknown map"):
        service.start("nope")


def test_move_and_bump(service):
    service.start("box")
    out = service.command("d")
    assert "hp=" in out
    # Walking into the right wall should bump and stay in place.
    out2 = service.command("d")
    assert "hp=" in out2


def test_shoot(service):
    service.start("box")
    # Turn toward the enemy so it's in the firing cone, then shoot.
    for _ in range(10):
        service.command("d")
        if service.can_shoot():
            break
    before = service._game.entities[0].health
    out = service.command("shoot")
    assert service._game.entities[0].health < before
    assert "hp=" in out


def test_turn(service):
    service.start("box")
    start_dir = service._game.player.dir.x
    service.command("d")
    assert service._game.player.dir.x != start_dir


def test_enemy_hits_player(service):
    service.start("box")
    # On the small box map, turn toward the enemy, walk forward until adjacent;
    # the enemy turn should then bite the player.
    for _ in range(20):
        service.command("d")
        if service.can_shoot():
            break
    for _ in range(120):
        service.command("w")
        if service._game.player.health < nano_doom.PLAYER_MAX_HEALTH:
            break
    hp = service._game.player.health
    assert hp < nano_doom.PLAYER_MAX_HEALTH


def test_module_loads_and_registers_doom(tmp_path):
    loader = _load_nano_doom(tmp_path, [])
    assert "nano-doom" in loader.modules
    assert loader.registry.get("doom") is not None


def test_module_slash_command_status(tmp_path):
    loader = _load_nano_doom(tmp_path, [])
    commands = loader.registry.get("commands")
    result = commands.dispatch("/doom", [])
    assert "no game running" in result


def test_module_slash_command_start(tmp_path):
    loader = _load_nano_doom(tmp_path, [])
    commands = loader.registry.get("commands")
    result = commands.dispatch("/doom", ["start"])
    assert "hp=" in result
    assert "enemies=" in result


def test_module_slash_command_direct_move(tmp_path):
    loader = _load_nano_doom(tmp_path, [])
    commands = loader.registry.get("commands")
    commands.dispatch("/doom", ["start"])
    result = commands.dispatch("/doom", ["w"])
    assert "hp=" in result


def test_module_events_declared(tmp_path):
    loader = _load_nano_doom(tmp_path, [])
    hooks = loader.registry.get("hooks")
    assert "doom_start" in hooks.known()
    assert "doom_stop" in hooks.known()
    assert "doom_tick" in hooks.known()


def test_module_utterance_dispatch(tmp_path, caplog):
    logs = []
    loader = _load_nano_doom(tmp_path, logs)
    hooks = loader.registry.get("hooks")
    hooks.emit("utterance", 'doom.start("box")')
    assert any("nano-doom: started box" in line for line in logs)
