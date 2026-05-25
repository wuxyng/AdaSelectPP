from __future__ import annotations

import re
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Set, Tuple

from adaselect_pp.common import norm_name, split_template_sql, unique_keep_order
from .types import QueryEvidence
from .vocabulary import ColumnVocabulary

logger = logging.getLogger(__name__)


class StaticSQLExtractor:
    """Extract static SQL evidence for candidate generation.

    This extractor deliberately does not call EXPLAIN and does not inspect the
    current physical configuration.  It extracts predicate/join/range evidence
    from SQL text using sqlglot when available, with a conservative regex
    fallback.
    """

    def __init__(self, db_con, vocabulary: Optional[ColumnVocabulary] = None, dialect: str = "postgres") -> None:
        self.db = db_con
        self.vocab = vocabulary or ColumnVocabulary()
        self.dialect = dialect
        self.tables: List[str] = [norm_name(t) for t in db_con.get_tables()]
        self.columns: Dict[str, Set[str]] = {}
        for t in self.tables:
            try:
                self.columns[t] = {norm_name(c) for c in db_con.get_columns(t)}
            except Exception:
                self.columns[t] = set()
        self._sqlglot = None
        self._exp = None
        try:
            import sqlglot
            from sqlglot import exp
            self._sqlglot = sqlglot
            self._exp = exp
        except Exception:
            self._sqlglot = None
            self._exp = None
        logger.info(
            "StaticSQLExtractor init | dialect=%s sqlglot_available=%s tables=%d columns=%d",
            self.dialect,
            self.sqlglot_available,
            len(self.tables),
            sum(len(cols) for cols in self.columns.values()),
        )

    @property
    def sqlglot_available(self) -> bool:
        return self._sqlglot is not None

    def extract_line(self, line: str, query_id: int) -> QueryEvidence:
        tid, sql = split_template_sql(line, f"q{query_id}")
        if self._sqlglot is not None:
            try:
                return self._extract_ast(sql, tid, query_id)
            except Exception as exc:
                ev = self._extract_regex(sql, tid, query_id)
                ev.warnings.append(f"ast_failed:{type(exc).__name__}")
                return ev
        return self._extract_regex(sql, tid, query_id)

    # ----------------------------- AST path -----------------------------
    def _extract_ast(self, sql: str, template_id: str, query_id: int) -> QueryEvidence:
        exp = self._exp
        tree = self._sqlglot.parse_one(sql, read=self.dialect)
        ev = QueryEvidence(query_id=query_id, template_id=str(template_id), sql=sql, parse_status="ast_ok")

        alias_to_table: Dict[str, str] = {}
        table_aliases: Dict[str, Set[str]] = defaultdict(set)
        table_order: List[str] = []
        for tbl in tree.find_all(exp.Table):
            name = norm_name(tbl.name)
            if name not in self.columns:
                continue
            alias = norm_name(tbl.alias_or_name) if tbl.alias_or_name else name
            alias_to_table[alias] = name
            alias_to_table[name] = name
            table_aliases[name].add(alias)
            if name not in table_order:
                table_order.append(name)
        ev.tables = set(table_order)
        ev.table_order = list(table_order)
        ev.has_or = any(True for _ in tree.find_all(exp.Or))
        ev.alias_ambiguous_tables = {t for t, aliases in table_aliases.items() if len(aliases) > 1}

        def add(mp: Dict[str, List[str]], table: str, col: str) -> None:
            t = norm_name(table)
            c = norm_name(col)
            if t and c and t in self.columns and c in self.columns[t] and self.vocab.is_allowed(t, c):
                mp.setdefault(t, []).append(c)

        def resolve_col(node) -> Optional[Tuple[str, str]]:
            if not isinstance(node, exp.Column):
                return None
            col = norm_name(node.name)
            qualifier = norm_name(node.table) if node.table else ""
            if qualifier:
                table = alias_to_table.get(qualifier, qualifier)
                if table in self.columns and col in self.columns[table] and self.vocab.is_allowed(table, col):
                    return table, col
                return None
            matches = [t for t in table_order if col in self.columns.get(t, set()) and self.vocab.is_allowed(t, col)]
            if len(matches) == 1:
                return matches[0], col
            return None

        def is_column(node) -> bool:
            return isinstance(node, exp.Column)

        def is_literalish(node) -> bool:
            if node is None:
                return False
            return not isinstance(node, exp.Column)

        # Equality predicates: column=column is join; column=literal/expression is filter equality.
        for pred in tree.find_all(exp.EQ):
            left = pred.left if hasattr(pred, "left") else pred.args.get("this")
            right = pred.right if hasattr(pred, "right") else pred.args.get("expression")
            lcol = resolve_col(left)
            rcol = resolve_col(right)
            if lcol and rcol:
                add(ev.join_eq, *lcol)
                add(ev.join_eq, *rcol)
            elif lcol and is_literalish(right):
                add(ev.filter_eq, *lcol)
            elif rcol and is_literalish(left):
                add(ev.filter_eq, *rcol)

        # IN is equality-like if left side is a column.
        for pred in tree.find_all(exp.In):
            col = resolve_col(pred.args.get("this"))
            if col:
                add(ev.filter_eq, *col)

        # Ranges.
        for cls in (exp.GT, exp.GTE, exp.LT, exp.LTE):
            for pred in tree.find_all(cls):
                left = pred.left if hasattr(pred, "left") else pred.args.get("this")
                right = pred.right if hasattr(pred, "right") else pred.args.get("expression")
                lcol = resolve_col(left)
                rcol = resolve_col(right)
                if lcol and not rcol:
                    add(ev.filter_rng, *lcol)
                elif rcol and not lcol:
                    add(ev.filter_rng, *rcol)
        for pred in tree.find_all(exp.Between):
            col = resolve_col(pred.args.get("this"))
            if col:
                add(ev.filter_rng, *col)
        for pred in tree.find_all(exp.Like):
            col = resolve_col(pred.args.get("this"))
            pattern = pred.args.get("expression")
            if col and self._is_prefix_like_literal(pattern):
                add(ev.filter_rng, *col)

        # Strong equality groups: only non-OR top-level AND factors with filter EQ/IN.
        # We keep this intentionally conservative.  Join equality is not used for EQ_EQ
        # unless paired with local range via EQ_RANGE/JOIN_RANGE later.
        if not ev.has_or:
            where = tree.args.get("where")
            if where is not None:
                for atoms in self._split_and_factors(where.this if hasattr(where, "this") else where):
                    by_table: Dict[str, List[str]] = defaultdict(list)
                    for atom in atoms:
                        if isinstance(atom, exp.EQ):
                            l = atom.left if hasattr(atom, "left") else atom.args.get("this")
                            r = atom.right if hasattr(atom, "right") else atom.args.get("expression")
                            lc = resolve_col(l)
                            rc = resolve_col(r)
                            if lc and not rc and is_literalish(r):
                                by_table[lc[0]].append(lc[1])
                            elif rc and not lc and is_literalish(l):
                                by_table[rc[0]].append(rc[1])
                        elif isinstance(atom, exp.In):
                            c = resolve_col(atom.args.get("this"))
                            if c:
                                by_table[c[0]].append(c[1])
                    for t, cols in by_table.items():
                        merged = ev.strong_factor_eq.get(t, []) + cols
                        ev.strong_factor_eq[t] = unique_keep_order(merged)

        for mp in (ev.filter_eq, ev.filter_rng, ev.join_eq, ev.strong_factor_eq):
            for t in list(mp):
                mp[t] = unique_keep_order(mp[t])
                if not mp[t]:
                    del mp[t]
        return ev

    def _is_prefix_like_literal(self, node) -> bool:
        exp = self._exp
        if node is None or not isinstance(node, exp.Literal):
            return False
        try:
            pattern = str(node.this)
        except Exception:
            return False
        return bool(pattern.endswith("%") and not pattern.startswith(("%", "_")))

    def _split_and_factors(self, expr) -> List[List[object]]:
        exp = self._exp
        if expr is None:
            return []
        if isinstance(expr, exp.Or):
            return []
        atoms: List[object] = []

        def rec(node):
            if isinstance(node, exp.And):
                rec(node.left)
                rec(node.right)
            else:
                atoms.append(node)

        rec(expr)
        return [atoms] if atoms else []

    # ----------------------------- regex fallback -----------------------------
    def _extract_regex(self, sql: str, template_id: str, query_id: int) -> QueryEvidence:
        q = sql.lower()
        ev = QueryEvidence(query_id=query_id, template_id=str(template_id), sql=sql, parse_status="fallback_regex")
        table_order = [t for t in self.tables if re.search(r"\b" + re.escape(t) + r"\b", q)]
        ev.tables = set(table_order)
        ev.table_order = list(table_order)
        ev.has_or = bool(re.search(r"\bor\b", q))

        def add(mp, t, c):
            if self.vocab.is_allowed(t, c):
                mp.setdefault(t, []).append(c)

        for t in table_order:
            for c in sorted(self.columns.get(t, set())):
                if not self.vocab.is_allowed(t, c):
                    continue
                if not re.search(r"\b" + re.escape(c) + r"\b", q):
                    continue
                if re.search(r"\b" + re.escape(c) + r"\s*=\s*", q):
                    add(ev.filter_eq, t, c)
                elif (
                    re.search(r"\b" + re.escape(c) + r"\s*(<=|>=|<|>)", q)
                    or re.search(r"\b" + re.escape(c) + r"\s+between\b", q)
                    or re.search(r"\b" + re.escape(c) + r"\s+like\s+'[^%_'][^']*%'", q)
                ):
                    add(ev.filter_rng, t, c)
                else:
                    add(ev.join_eq, t, c)
        for mp in (ev.filter_eq, ev.filter_rng, ev.join_eq):
            for t in list(mp):
                mp[t] = unique_keep_order(mp[t])
                if not mp[t]:
                    del mp[t]
        return ev
