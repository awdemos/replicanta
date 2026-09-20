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
    config = {
        "modules": {"enabled": ["base", "nano-doom"]},
        "persona": {},
    }
    loader = ModuleLoader(tmp_path / "modules", organism=None, config=config, emit=logs.append)
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


def test_shoot_reduces_ammo(service):
    service.start("default")
    before = service._game.player.ammo
    out = service.command("shoot")
    assert service._game.player.ammo == before - 1
    assert "ammo=" in out


def test_turn(service):
    service.start("box")
    start_angle = service._game.player.angle
    service.command("e")
    assert service._game.player.angle > start_angle


def test_enemy_hits_player(service):
    service.start("box")
    # On the small box map, strafe/move toward the enemy until adjacent;
    # the enemy turn should then bite the player. Weaker bite (1 hp) makes the
    # loop longer, so just assert hp dropped below the starting 15.
    for _ in range(12):
        service.command("d")
        if service._game.player.hp < 15:
            break
    hp = service._game.player.hp
    assert hp < 15


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
    assert "target=" in result


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
