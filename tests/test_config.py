from replicanta import config


def test_load_config_missing_file_returns_defaults(tmp_path):
    cfg = config.load_config(tmp_path)
    assert cfg["git"]["enabled"] is False
    assert cfg["git"]["dirty_many_at"] == 15
    assert cfg["git"]["behind_many_weight"] == 0.10


def test_load_config_reads_user_values(tmp_path):
    (tmp_path / "replicanta.toml").write_text("[git]\nenabled = true\ndirty_many_at = 99\n")
    cfg = config.load_config(tmp_path)
    assert cfg["git"]["enabled"] is True
    assert cfg["git"]["dirty_many_at"] == 99
    assert cfg["git"]["unpushed_many_at"] == 5  # default preserved


def test_load_config_malformed_file_returns_defaults(tmp_path, caplog):
    (tmp_path / "replicanta.toml").write_text("[git\nenabled = true\n")
    with caplog.at_level("WARNING"):
        cfg = config.load_config(tmp_path)
    assert cfg["git"]["enabled"] is False
    assert "cannot read" in caplog.text


def test_save_config_roundtrip(tmp_path):
    cfg = config.load_config(tmp_path)
    cfg["git"]["enabled"] = True
    cfg["git"]["dirty_many_at"] = 42
    config.save_config(tmp_path, cfg)
    loaded = config.load_config(tmp_path)
    assert loaded["git"]["enabled"] is True
    assert loaded["git"]["dirty_many_at"] == 42


def test_save_config_preserves_unrelated_section(tmp_path):
    (tmp_path / "replicanta.toml").write_text('[voice]\nmodel = "alan"\n')
    cfg = config.load_config(tmp_path)
    cfg["git"]["enabled"] = True
    config.save_config(tmp_path, cfg)
    loaded = config.load_config(tmp_path)
    assert loaded["voice"]["model"] == "alan"
    assert loaded["git"]["enabled"] is True


def test_save_config_escapes_strings(tmp_path):
    # Quotes/newlines in a value must not corrupt the file or inject keys.
    cfg = config.load_config(tmp_path)
    cfg["persona"] = {"name": 'evil"\ninjected = true\nx = "'}
    config.save_config(tmp_path, cfg)
    loaded = config.load_config(tmp_path)
    assert loaded["persona"]["name"] == 'evil"\ninjected = true\nx = "'
    assert "injected" not in loaded
    assert "x" not in loaded


def test_save_config_renders_scalars_as_before(tmp_path):
    cfg = config.load_config(tmp_path)
    cfg["voice"] = {"enabled": True, "volume": 3, "gain": 0.5, "model": "alan"}
    config.save_config(tmp_path, cfg)
    loaded = config.load_config(tmp_path)
    assert loaded["voice"] == {"enabled": True, "volume": 3, "gain": 0.5, "model": "alan"}


def test_save_config_none_and_nested_values_never_corrupt_file(tmp_path, caplog):
    """None used to render as `key = None` and deeply nested dicts as a
    Python repr — both invalid TOML that broke the next load. They must be
    commented/skipped instead, and the file must stay parseable."""
    cfg = config.load_config(tmp_path)
    cfg["git"]["enabled"] = None
    cfg["persona"] = {"name": "fern", "meta": {"deep": {"x": 1}}}
    with caplog.at_level("WARNING"):
        config.save_config(tmp_path, cfg)
    text = (tmp_path / "replicanta.toml").read_text()
    assert "enabled = None" not in text
    assert "'deep'" not in text  # no Python repr leaked into the file
    loaded = config.load_config(tmp_path)  # must parse
    assert loaded["git"]["enabled"] is False  # omitted key falls back to default
    assert loaded["persona"]["name"] == "fern"
    assert "meta" not in loaded["persona"]  # unrenderable inline value skipped
    assert "unsupported" in caplog.text


def test_save_config_list_of_scalars_roundtrips(tmp_path):
    """The module manager persists `modules.enabled` as a TOML array;
    lists of scalars must survive save/load (regression: they rendered
    as a comment, silently discarding the user's module toggles)."""
    cfg = config.load_config(tmp_path)
    cfg["modules"] = {"enabled": ["base", "fly-brain", "nano-doom"]}
    config.save_config(tmp_path, cfg)
    text = (tmp_path / "replicanta.toml").read_text()
    assert 'enabled = ["base", "fly-brain", "nano-doom"]' in text
    loaded = config.load_config(tmp_path)
    assert loaded["modules"]["enabled"] == ["base", "fly-brain", "nano-doom"]


def test_save_config_list_with_unrenderable_element_is_commented(tmp_path, caplog):
    cfg = config.load_config(tmp_path)
    cfg["modules"] = {"enabled": ["base", {"bad": "dict"}]}
    with caplog.at_level("WARNING"):
        config.save_config(tmp_path, cfg)
    loaded = config.load_config(tmp_path)
    assert "enabled" not in loaded["modules"]
    assert "unrenderable list element" in caplog.text
