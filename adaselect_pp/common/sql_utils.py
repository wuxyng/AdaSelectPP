from __future__ import annotations

import re
from typing import Tuple

SQL_START_RE = re.compile(r"^\s*(select|with|insert|update|delete|explain)\b", re.I)


def looks_like_sql(text: str) -> bool:
    return bool(SQL_START_RE.search(text or ""))


def split_template_sql(line: str, fallback_id: str = "") -> Tuple[str, str]:
    """Return (template_id, sql) from either '<sql>\t<tid>' or '<tid>\t<sql>'.

    Workload files in this project commonly store '<SQL>\t<template_id>'.  The
    internal canonical form is '<template_id>\t<SQL>'.  This helper accepts both
    and never sends the template id to PostgreSQL.
    """
    raw = (line or "").strip()
    if "\t" not in raw:
        return fallback_id, raw
    a, b = raw.split("\t", 1)
    a = a.strip()
    b = b.strip()
    if looks_like_sql(a) and not looks_like_sql(b):
        return b or fallback_id, a
    if looks_like_sql(b):
        return a or fallback_id, b
    return fallback_id, raw


def sql_only(line: str) -> str:
    return split_template_sql(line, "")[1]


def canonical_workload_line(line: str, fallback_id: str = "") -> str:
    tid, sql = split_template_sql(line, fallback_id)
    if tid:
        return f"{tid}\t{sql}"
    return sql


def norm_name(name: str) -> str:
    return str(name or "").strip().strip('"').lower()


def unique_keep_order(values):
    out = []
    seen = set()
    for v in values or []:
        vv = norm_name(v)
        if vv and vv not in seen:
            seen.add(vv)
            out.append(vv)
    return out
