from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional, Sequence, Set

from adaselect_pp.common import norm_name

logger = logging.getLogger(__name__)


class ColumnVocabulary:
    """Benchmark-specific indexable-column whitelist.

    The vocabulary is a whitelist, not a candidate source.  If absent or empty,
    all schema-valid columns are allowed.  Multiple legacy text formats are
    supported, including 'table col', 'col table', 'table.col', 'table: c1 c2',
    and 'table(c1,c2)'.  Schema validation is used to disambiguate order.
    """

    def __init__(
        self,
        mapping: Optional[Dict[str, Set[str]]] = None,
        *,
        path: Optional[Path] = None,
        candidate_paths: Sequence[Path] = (),
        status: str = "not_loaded",
    ) -> None:
        self.mapping: Dict[str, Set[str]] = {
            norm_name(t): {norm_name(c) for c in cols if norm_name(c)}
            for t, cols in (mapping or {}).items()
            if norm_name(t)
        }
        self.enabled = bool(self.mapping)
        self.path = str(path) if path is not None else ""
        self.candidate_paths = [str(p) for p in candidate_paths]
        self.status = status

    def is_allowed(self, table: str, column: str) -> bool:
        if not self.enabled:
            return True
        return norm_name(column) in self.mapping.get(norm_name(table), set())

    def allowed_columns(self, table: str) -> Optional[Set[str]]:
        if not self.enabled:
            return None
        return set(self.mapping.get(norm_name(table), set()))

    @classmethod
    def load(
        cls,
        benchmark: str,
        db_con=None,
        explicit_path: str = "",
        *,
        required: bool = False,
    ) -> "ColumnVocabulary":
        candidates = []
        if explicit_path:
            candidates.append(Path(explicit_path))
        else:
            bench = norm_name(benchmark)
            candidates.extend([
                Path("txt") / f"{bench}_indexable_columns.txt",
                Path("database") / "txt" / f"{bench}_indexable_columns.txt",
                Path(f"{bench}_indexable_columns.txt"),
            ])
        path = next((p for p in candidates if p.exists()), None)
        if path is None:
            msg = (
                f"indexable-column whitelist not found for benchmark={benchmark!r}; "
                f"checked={[str(p) for p in candidates]}"
            )
            if required:
                raise FileNotFoundError(msg)
            logger.warning(msg)
            return cls(candidate_paths=candidates, status="missing")

        schema: Dict[str, Set[str]] = {}
        schema_error = False
        if db_con is not None:
            try:
                for t in db_con.get_tables():
                    tt = norm_name(t)
                    schema[tt] = {norm_name(c) for c in db_con.get_columns(t)}
            except Exception:
                schema_error = True
                schema = {}
            schema_unavailable = not schema or all(not cols for cols in schema.values())
            if required and (schema_error or schema_unavailable):
                raise RuntimeError(
                    "schema metadata unavailable while loading required indexable-column whitelist "
                    f"for benchmark={benchmark!r}: {path}"
                )
            if schema_error or schema_unavailable:
                logger.warning(
                    "Schema metadata unavailable while loading indexable-column whitelist | benchmark=%s path=%s",
                    benchmark,
                    path,
                )

        mapping: Dict[str, Set[str]] = {}

        def add(table: str, col: str) -> None:
            t = norm_name(table)
            c = norm_name(col)
            if not t or not c:
                return
            if schema and (t not in schema or c not in schema[t]):
                return
            mapping.setdefault(t, set()).add(c)

        def add_pair(a: str, b: str) -> None:
            aa = norm_name(a)
            bb = norm_name(b)
            if not aa or not bb:
                return
            if schema:
                if aa in schema and bb in schema[aa]:
                    add(aa, bb)
                elif bb in schema and aa in schema[bb]:
                    add(bb, aa)
            else:
                add(aa, bb)

        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                t, rest = line.split(":", 1)
                for c in rest.replace(",", " ").split():
                    add(t, c)
                continue
            if "(" in line and ")" in line:
                t = line.split("(", 1)[0]
                rest = line.split("(", 1)[1].rsplit(")", 1)[0]
                for c in rest.replace(",", " ").split():
                    add(t, c)
                continue
            toks = line.replace(",", " ").split()
            if len(toks) == 1 and "." in toks[0]:
                a, b = toks[0].split(".", 1)
                add_pair(a, b)
            elif len(toks) >= 2:
                # Some legacy files use '<column> <table>', others '<table> <column>'.
                add_pair(toks[0], toks[1])

        vocab = cls(mapping, path=path, candidate_paths=candidates, status="loaded" if mapping else "empty")
        if not vocab.enabled:
            msg = f"indexable-column whitelist parsed empty for benchmark={benchmark!r}: {path}"
            if required:
                raise ValueError(msg)
            logger.warning(msg)
        logger.info(
            "Whitelist loaded | benchmark=%s path=%s enabled=%s tables=%d columns=%d status=%s",
            benchmark,
            vocab.path,
            vocab.enabled,
            len(vocab.mapping),
            sum(len(cols) for cols in vocab.mapping.values()),
            vocab.status,
        )
        return vocab
