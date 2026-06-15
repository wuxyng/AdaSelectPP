"""Lightweight config flag helpers for AdaSelect CLI glue."""

from __future__ import annotations

import re


_TARGET_PAIR_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*([^,()\s]+)\s*,\s*([^,()\s]+)\s*\)\s*$")


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


def resolve_pair_supply_ceiling_enabled(cli_value, env_value, config_value, default=False) -> bool:
    if cli_value is not None:
        return coerce_bool_flag(cli_value)
    if env_value is not None:
        return coerce_bool_flag(env_value)
    if config_value is not None:
        return coerce_bool_flag(config_value)
    return bool(default)


def resolve_pair_supply_fairness_enabled(cli_value, env_value, config_value, default=False) -> bool:
    if cli_value is not None:
        return coerce_bool_flag(cli_value)
    if env_value is not None:
        return coerce_bool_flag(env_value)
    if config_value is not None:
        return coerce_bool_flag(config_value)
    return bool(default)


def resolve_int_flag(cli_value, env_value, config_value, default: int) -> int:
    if cli_value is not None:
        return int(cli_value)
    if env_value is not None and str(env_value).strip() != "":
        return int(env_value)
    if config_value is not None:
        return int(config_value)
    return int(default)


def resolve_target_pair_audit(cli_value, env_value, config_value, default="") -> str:
    if cli_value is not None:
        return str(cli_value)
    if env_value is not None:
        return str(env_value)
    if config_value is not None:
        if isinstance(config_value, (list, tuple, set)):
            return ";".join(str(v) for v in config_value)
        return str(config_value)
    return str(default or "")


def parse_target_pair_audit(value):
    if not value:
        return set()
    if isinstance(value, set):
        items = value
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        items = [part for part in str(value).split(";") if part.strip()]
    pairs = set()
    for item in items:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], tuple) and len(item[1]) == 2:
            table = str(item[0]).strip().lower()
            cols = tuple(str(c).strip().lower() for c in item[1])
            pairs.add((table, cols))
            continue
        match = _TARGET_PAIR_RE.match(str(item))
        if not match:
            raise ValueError(f"invalid target pair audit entry: {item!r}")
        table, col1, col2 = match.groups()
        pairs.add((table.lower(), (col1.lower(), col2.lower())))
    return pairs
