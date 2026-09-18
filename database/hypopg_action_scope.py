"""Session-local, catalog-validated full configurations for action experiments.

Only EXPLAIN, HypoPG and catalog reads. No pg_index writes or physical DDL.
The connection is exclusively owned and discarded on any cleanup failure.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from tools.evaluation_substrate.schema import IndexDefinition
from tools.evaluation_substrate.reveal import (
    _HypoPGSession, OptimizerEvaluation, OptimizerRevealError, _safe_explain_query,
    _normalize_plan_value, _sha256_json, _used_indexes, _quote_ident,
)


class UnsupportedBackend(OptimizerRevealError):
    pass


class ContaminatedSession(OptimizerRevealError):
    pass


@dataclass(frozen=True)
class PhysicalIndex:
    definition: IndexDefinition
    oid: int
    name: str
    protected: bool
    ddl: str


class HypoPGActionSession(_HypoPGSession):
    def __init__(self, connection, relevant_relations):
        super().__init__(connection)
        self.relevant_relations = tuple(relevant_relations)
        self.contaminated = False
        self.explain_attempts = 0
        try:
            self.capabilities = self._capabilities()
            self.physical = self._catalog()
            self._assert_clean()
        except BaseException:
            connection.close()
            raise

    def _capabilities(self):
        row = self._fetchone("SELECT e.extversion, n.nspname FROM pg_extension e "
                             "JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='hypopg'")
        if not row:
            raise UnsupportedBackend("UNSUPPORTED_BACKEND: HypoPG is not installed")
        version, schema = map(str, row)
        self.schema = schema
        rows = self._fetchall("SELECT p.proname, oidvectortypes(p.proargtypes), "
            "p.prorettype::regtype::text, p.proretset FROM pg_proc p "
            "JOIN pg_depend d ON d.objid=p.oid AND d.classid='pg_proc'::regclass "
            "JOIN pg_extension e ON e.oid=d.refobjid AND d.refclassid='pg_extension'::regclass "
            "JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE e.extname='hypopg' AND d.deptype='e' AND n.nspname=%s", (schema,))
        found = {(name, args): (result, many) for name, args, result, many in rows}
        required = {
            ("hypopg_hide_index", "oid"): ("boolean", False),
            ("hypopg_unhide_index", "oid"): ("boolean", False),
            ("hypopg_hidden_indexes", ""): ("oid", True),
            ("hypopg_reset", ""): ("void", False),
            ("hypopg_create_index", "text"): ("record", True),
            ("hypopg_get_indexdef", "oid"): ("text", False),
        }
        missing = [f"{name}({args})" for (name, args), contract in required.items()
                   if found.get((name, args)) != contract]
        if missing:
            raise UnsupportedBackend("UNSUPPORTED_BACKEND: missing/incompatible " + ", ".join(missing))
        # Actually invoke list/reset; an extension version number proves nothing.
        if self._hidden() or self._list_hypopg_state():
            raise UnsupportedBackend("measurement connection must start with empty HypoPG state")
        self._fetchone(f"SELECT {self._fn('hypopg_reset')}()")
        if self._hidden() or self._list_hypopg_state():
            raise UnsupportedBackend("HypoPG reset/list contract failed")
        return {"version": version, "schema": schema, "functions": sorted(f"{n}({a})" for n, a in required)}

    def _fn(self, name):
        return f"{_quote_ident(self.schema)}.{_quote_ident(name)}"

    def _hidden(self):
        return tuple(sorted(int(r[0]) for r in self._fetchall(
            f"SELECT * FROM {self._fn('hypopg_hidden_indexes')}()")))

    def _list_hypopg_state(self):
        rows = self._fetchall(f"SELECT indexrelid::text, {self._fn('hypopg_get_indexdef')}(indexrelid)::text "
                              f"FROM {self._fn('hypopg_list_indexes')} ORDER BY indexrelid")
        return tuple((str(r[0]), str(r[1])) for r in rows)

    def _assert_clean(self):
        if self.contaminated or self._hidden() or self._list_hypopg_state():
            raise ContaminatedSession("measurement session is not clean")

    def _catalog(self):
        indexes = {}
        for schema, table in self.relevant_relations:
            rows = self._fetchall("SELECT i.indexrelid, ci.relname, pg_get_indexdef(i.indexrelid), "
                "i.indisunique OR i.indisprimary, i.indisvalid AND i.indisready, ci.relkind, am.amname, "
                "i.indexprs IS NULL AND i.indpred IS NULL AND i.indnatts=i.indnkeyatts, "
                "ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(attnum,n) "
                "LEFT JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum ORDER BY k.n), "
                "NOT EXISTS(SELECT 1 FROM unnest(i.indclass) c(oid) JOIN pg_opclass o ON o.oid=c.oid WHERE NOT o.opcdefault), "
                "NOT EXISTS(SELECT 1 FROM unnest(i.indoption) x(v) WHERE x.v<>0), "
                "NOT EXISTS(SELECT 1 FROM unnest(i.indcollation) WITH ORDINALITY c(oid,n) "
                "JOIN unnest(i.indkey) WITH ORDINALITY k(attnum,n) USING(n) "
                "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum WHERE c.oid<>a.attcollation) "
                "FROM pg_index i JOIN pg_class ct ON ct.oid=i.indrelid JOIN pg_namespace n ON n.oid=ct.relnamespace "
                "JOIN pg_class ci ON ci.oid=i.indexrelid JOIN pg_am am ON am.oid=ci.relam "
                "WHERE n.nspname=%s AND ct.relname=%s ORDER BY i.indexrelid", (schema, table))
            for oid, name, ddl, protected, valid, kind, am, simple, columns, opclass, ordering, collation in rows:
                if (schema != "public" or not valid or kind != "i" or am != "btree"
                        or not all((simple, opclass, ordering, collation)) or not all(columns)):
                    raise UnsupportedBackend(f"unsupported physical index identity: {ddl}")
                definition = IndexDefinition(table, tuple(columns))
                if definition in indexes:
                    raise UnsupportedBackend(f"duplicate physical index definition: {definition.canonical_text}")
                indexes[definition] = PhysicalIndex(definition, int(oid), str(name), bool(protected), str(ddl))
        return indexes

    @property
    def protected(self):
        return frozenset(k for k, v in self.physical.items() if v.protected)

    def _capture_epoch(self, relevant_relations):
        self._assert_clean()
        if self._catalog() != self.physical:
            raise OptimizerRevealError("physical catalog changed within action window")
        return super()._capture_epoch(relevant_relations)

    def _evaluate(self, query, configuration):
        self._assert_clean()
        target = set(configuration)
        if not self.protected <= target:
            raise OptimizerRevealError("protected indexes cannot be hidden")
        if self._catalog() != self.physical:
            raise OptimizerRevealError("actual root changed before evaluation")
        hidden, expected, names, oids = [], [], {}, {}
        try:
            for index in sorted(set(self.physical) - target):
                oid = self.physical[index].oid
                hidden.append(oid)  # Before the call: even a lost reply needs cleanup.
                row = self._fetchone(f"SELECT {self._fn('hypopg_hide_index')}(%s::oid)", (oid,))
                if row != (True,):
                    raise UnsupportedBackend("hide_index did not return true")
            for index in sorted(target - set(self.physical)):
                row = self._fetchone(f"SELECT * FROM {self._fn('hypopg_create_index')}(%s)", (self._definition(index),))
                if not row or len(row) != 2 or row[0] is None or row[1] is None:
                    raise UnsupportedBackend("create_index did not return (oid, name)")
                oid, name = str(row[0]), str(row[1])
                expected.append((oid, index))
                names[name] = oids[oid] = "hypopg:" + index.canonical_text
            installed = self._list_hypopg_state()
            actual = sorted((oid, self._parse_installed_definition(ddl)) for oid, ddl in installed)
            if actual != sorted(expected) or self._hidden() != tuple(sorted(hidden)):
                raise OptimizerRevealError("full target HypoPG state does not match request")
            sql = _safe_explain_query(query.sql)
            self.explain_attempts = getattr(self, "explain_attempts", 0) + 1
            row = self._fetchone("EXPLAIN (FORMAT JSON) " + sql)
            payload = _normalize_plan_value(self._plan_payload(row[0]), names, oids)
            return OptimizerEvaluation(float(payload["Plan"]["Total Cost"]), _used_indexes(payload), _sha256_json(payload))
        finally:
            errors = []
            try:
                self._rollback()
            except BaseException as exc:
                errors.append(exc)
            for oid in hidden:
                try:
                    self._fetchone(f"SELECT {self._fn('hypopg_unhide_index')}(%s::oid)", (oid,))
                except BaseException as exc:
                    errors.append(exc)
            try:
                self._fetchone(f"SELECT {self._fn('hypopg_reset')}()")
                self._assert_clean()
                if self._catalog() != self.physical:
                    raise ContaminatedSession("actual index catalog changed during measurement")
            except BaseException as exc:
                errors.append(exc)
            if errors:
                self.contaminated = True
                self._connection.close()
                raise ContaminatedSession("cleanup failed; measurement connection discarded: " + str(errors[0]))
