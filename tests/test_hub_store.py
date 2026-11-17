"""One contract, two stores: the in-memory fake and the SQLite (WAL) store must behave identically."""
from __future__ import annotations

import pytest

from twin_agent.hub.store import MemoryStore, SqliteStore


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    s = MemoryStore() if request.param == "memory" else SqliteStore(str(tmp_path / "hub.db"))
    yield s
    s.close()


def test_put_get_roundtrip_and_isolation(store):
    doc = {"a": [1, 2], "b": {"c": "ع"}}
    store.put("t", "1", doc)
    got = store.get("t", "1")
    assert got == doc
    got["a"].append(3)                                   # mutating a read copy must not touch stored state
    assert store.get("t", "1") == doc
    assert store.get("t", "missing") is None


def test_put_if_absent_is_first_writer_wins(store):
    assert store.put_if_absent("m", "x", {"v": 1}) is True
    assert store.put_if_absent("m", "x", {"v": 2}) is False
    assert store.get("m", "x") == {"v": 1}


def test_update_is_read_modify_write_and_missing_is_none(store):
    store.put("a", "n", {"used": False})
    out = store.update("a", "n", lambda d: {**d, "used": True})
    assert out == {"used": True} and store.get("a", "n") == {"used": True}
    assert store.update("a", "nope", lambda d: d) is None


def test_update_that_raises_changes_nothing(store):
    store.put("a", "n", {"v": 1})

    def boom(d):
        d["v"] = 99
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        store.update("a", "n", boom)
    assert store.get("a", "n") == {"v": 1}


def test_scan_keeps_insertion_order_even_after_update(store):
    for k in ("3", "1", "2"):
        store.put("t", k, {"k": k})
    store.put("t", "3", {"k": "3", "again": True})
    assert [d["k"] for d in store.scan("t")] == ["3", "1", "2"]
    assert store.scan("empty") == []


def test_sqlite_state_survives_reopen_and_uses_wal(tmp_path):
    p = str(tmp_path / "hub.db")
    s = SqliteStore(p)
    s.put("tickets", "7", {"stage": "awaiting_customer"})
    assert s._c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    s.close()
    s2 = SqliteStore(p)
    assert s2.get("tickets", "7") == {"stage": "awaiting_customer"}
    s2.close()
