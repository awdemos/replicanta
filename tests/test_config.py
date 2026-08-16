from replicanta import config


def test_load_config_missing_file_returns_defaults(tmp_path):
    cfg = config.load_config(tmp_path)
    assert cfg["git"]["enabled"] is False
    assert cfg["git"]["dirty_many_at"] == 15
    assert cfg["git"]["behind_many_weight"] == 0.10


def test_load_config_reads_user_values(tmp_path):
    (tmp_path / "replicanta.toml").write_text(
        '[git]\nenabled = true\ndirty_many_at = 99\n'
    )
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
