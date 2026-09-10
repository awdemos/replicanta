from pathlib import Path

from replicanta.modules import ModuleLoader


def test_builtin_persona_modules_load(tmp_path):
    # Copy built-in modules into temp dir
    import shutil
    src = Path(__file__).parent.parent / "modules"
    if src.is_dir():
        shutil.copytree(src, tmp_path / "modules", dirs_exist_ok=True)
    config = {
        "modules": {"enabled": ["base", "software-engineer", "creative-writer", "socratic-philosopher"]},
        "persona": {},
    }
    loader = ModuleLoader(tmp_path / "modules", organism=None, config=config)
    loader.load_all()
    svc = loader.registry.get("persona")
    assert set(svc.list()) == {"software-engineer", "creative-writer", "socratic-philosopher"}
