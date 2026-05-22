from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Tuple

IndexKey = Tuple[str, Tuple[str, ...]]


@dataclass
class QueryEvidence:
    query_id: int
    template_id: str
    sql: str
    tables: Set[str] = field(default_factory=set)
    filter_eq: Dict[str, List[str]] = field(default_factory=dict)
    filter_rng: Dict[str, List[str]] = field(default_factory=dict)
    join_eq: Dict[str, List[str]] = field(default_factory=dict)
    strong_factor_eq: Dict[str, List[str]] = field(default_factory=dict)
    table_order: List[str] = field(default_factory=list)
    has_or: bool = False
    parse_status: str = "fallback_regex"  # ast_ok | fallback_regex | failed
    warnings: List[str] = field(default_factory=list)


@dataclass
class Candidate:
    key: IndexKey
    family: str
    source: str
    confidence: float
    roles: Tuple[str, ...] = ()
    query_ids: Set[int] = field(default_factory=set)
    template_ids: Set[str] = field(default_factory=set)
    support_count: int = 0
    score: float = 0.0


@dataclass
class GenerationResult:
    query_indexes: List[Set[IndexKey]]
    topk_set: Set[IndexKey]
    score_map: Dict[IndexKey, float]
    meta_map: Dict[IndexKey, Dict[str, Any]]
    stats: Dict[str, Any]
