from types import SimpleNamespace

import pytest

from database.hypopg_action_scope import HypoPGActionSession, PhysicalIndex, ContaminatedSession, UnsupportedBackend
from database.core_backend import config
from tools.evaluation_substrate import IndexDefinition, QuerySpec

P, U, PK = IndexDefinition("t", ("a",)), IndexDefinition("t", ("a", "b")), IndexDefinition("t", ("id",))


class MockSession(HypoPGActionSession):
    def __init__(self):
        self.closed = False
        self._connection = SimpleNamespace(close=lambda: setattr(self, "closed", True))
        self.schema = "private_extension"
        self.contaminated = False
        self.physical = {P: PhysicalIndex(P, 1, "p", False, "p"), PK: PhysicalIndex(PK, 2, "pk", True, "pk")}
        self.hidden, self.hypo, self.events = set(), {}, []
        self.fail_explain = None
        self.fail_cleanup = False

    def _catalog(self):
        return self.physical.copy()

    def _hidden(self):
        return tuple(sorted(self.hidden))

    def _list_hypopg_state(self):
        return tuple(sorted(self.hypo.items()))

    def _rollback(self):
        self.events.append("rollback")

    def _fetchone(self, sql, params=None):
        self.events.append(sql)
        assert "UPDATE pg_index" not in sql and "ANALYZE" not in sql
        if "hypopg_unhide_index" in sql:
            if self.fail_cleanup:
                raise RuntimeError("unhide failed")
            self.hidden.discard(params[0])
            return (True,)
        if "hypopg_hide_index" in sql:
            self.hidden.add(params[0])
            return (True,)
        if "hypopg_create_index" in sql:
            self.hypo["10"] = params[0]
            return (10, "hypo")
        if "hypopg_reset" in sql:
            self.hypo.clear()  # Does NOT unhide real indexes.
            return (None,)
        if sql.startswith("EXPLAIN"):
            assert self.hidden == {1} and len(self.hypo) == 1
            if self.fail_explain:
                raise self.fail_explain
            return ([{"Plan": {"Total Cost": 80}}],)
        raise AssertionError(sql)


def test_same_session_full_target_and_explicit_real_unhide():
    s = MockSession()
    assert s._evaluate(QuerySpec("q", "select 1"), config((PK, U))).optimizer_cost == 80
    assert not s.hidden and not s.hypo and not s.closed
    assert sum("hypopg_unhide_index" in e for e in s.events) == 1


@pytest.mark.parametrize("failure", [RuntimeError("timeout"), KeyboardInterrupt()])
def test_timeout_and_cancel_restore_root(failure):
    s = MockSession()
    s.fail_explain = failure
    with pytest.raises(type(failure)):
        s._evaluate(QuerySpec("q", "select 1"), config((PK, U)))
    assert not s.hidden and not s.hypo


def test_cleanup_failure_discards_connection_and_result():
    s = MockSession()
    s.fail_cleanup = True
    with pytest.raises(ContaminatedSession):
        s._evaluate(QuerySpec("q", "select 1"), config((PK, U)))
    assert s.closed and s.contaminated


def test_protected_not_hidden():
    s = MockSession()
    with pytest.raises(RuntimeError, match="protected"):
        s._evaluate(QuerySpec("q", "select 1"), config((U,)))
    assert not s.hidden


def test_capabilities_cannot_be_inferred_from_version():
    s = MockSession()
    s._fetchone = lambda *_: ("9.99", "private_extension")
    s._fetchall = lambda *_: []
    with pytest.raises(UnsupportedBackend, match="missing/incompatible"):
        s._capabilities()
