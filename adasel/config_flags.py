"""Lightweight config flag helpers for AdaSelect CLI glue."""

from __future__ import annotations


def coerce_bool_flag(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(f"invalid boolean flag value: {value!r}")


def resolve_replacement_overlay_enabled(cli_value, env_value, config_value, default=False) -> bool:
    if cli_value is not None:
        return coerce_bool_flag(cli_value)
    if env_value is not None:
        return coerce_bool_flag(env_value)
    if config_value is not None:
        return coerce_bool_flag(config_value)
    return bool(default)
