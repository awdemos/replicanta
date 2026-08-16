"""Project configuration loader for replicanta.toml."""

import logging
import tomllib
from pathlib import Path

from replicanta.fileutil import atomic_write_text

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "git": {
        "enabled": False,
        "dirty_many_at": 15,
        "unpushed_many_at": 5,
        "behind_many_at": 20,
        "dirty_weight": 0.05,
        "dirty_many_weight": 0.08,
        "unpushed_weight": 0.05,
        "unpushed_many_weight": 0.08,
        "behind_weight": 0.05,
        "behind_many_weight": 0.10,
    }
}


def config_path(root):
    return Path(root) / "replicanta.toml"


def load_config(root):
    """Load replicanta.toml from root, merging defaults over user values.
    Missing or malformed files fall back to defaults (a warning is logged
    for malformed files). Unknown sections from the user file are preserved."""
    path = config_path(root)
    if not path.is_file():
        return _copy(DEFAULT_CONFIG)
    try:
        with path.open("rb") as f:
            user = tomllib.load(f)
    except Exception as exc:  # noqa: BLE001 — config errors must not crash boot
        logger.warning("cannot read %s: %s; using defaults", path, exc)
        return _copy(DEFAULT_CONFIG)
    merged = _copy(user)
    _merge_defaults(merged, DEFAULT_CONFIG)
    return merged


def save_config(root, config):
    """Write config back to replicanta.toml. Preserves top-level sections
    and handles bool, int, float, str, and flat-dict values."""
    atomic_write_text(config_path(root), _render_config(config))


def _copy(cfg):
    return {k: dict(v) if isinstance(v, dict) else v for k, v in cfg.items()}


def _merge_defaults(user, defaults):
    for key, default_val in defaults.items():
        if isinstance(default_val, dict):
            user_val = user.setdefault(key, {})
            if isinstance(user_val, dict):
                for sub_key, sub_default in default_val.items():
                    user_val.setdefault(sub_key, sub_default)
        elif key not in user:
            user[key] = default_val


def _render_config(config):
    lines = []
    for section, values in config.items():
        if isinstance(values, dict):
            lines.append(f"[{section}]")
            for key, val in values.items():
                lines.append(_render_key_value(key, val))
            lines.append("")
        else:
            lines.append(_render_key_value(section, values))
            lines.append("")
    return "\n".join(lines)


def _render_key_value(key, val):
    if isinstance(val, bool):
        return f"{key} = {'true' if val else 'false'}"
    elif isinstance(val, str):
        return f'{key} = "{val}"'
    elif isinstance(val, dict):
        pairs = ", ".join(f"{k} = {_render_literal(v)}" for k, v in val.items())
        return f"{key} = {{ {pairs} }}"
    return f"{key} = {val}"


def _render_literal(val):
    if isinstance(val, bool):
        return "true" if val else "false"
    elif isinstance(val, str):
        return f'"{val}"'
    return str(val)
