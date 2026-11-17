"""Episodic memory: append-only timeline, query by request/ticket/client/time, redaction."""
from __future__ import annotations

from twin_agent.memory import episodes
from twin_agent.memory.store import MemoryDB


def test_append_and_timeline_by_request(tmp_home, clock):
    mem = MemoryDB(clock=clock)
    episodes.append(mem, request_ref="REQ-1", kind="opened", summary="new request", client="EG", ts="2026-11-01T08:00:00+00:00")
    episodes.append(mem, request_ref="REQ-1", kind="diagnosed", summary="matched stray-in-line", client="EG", ts="2026-11-01T09:00:00+00:00")
    episodes.append(mem, request_ref="REQ-2", kind="opened", summary="other", client="EG", ts="2026-11-01T10:00:00+00:00")
    tl = episodes.timeline(mem.conn, request_ref="REQ-1")
    assert [e["kind"] for e in tl] == ["opened", "diagnosed"]  # oldest first
    mem.close()


def test_timeline_filters(tmp_home, clock):
    mem = MemoryDB(clock=clock)
    episodes.append(mem, request_ref="REQ-1", ticket_ref="TKT-9", client="EG", kind="ticket", summary="raised", ts="2026-11-01T08:00:00+00:00")
    episodes.append(mem, request_ref="REQ-3", client="KSA", kind="opened", summary="ksa", ts="2026-11-02T08:00:00+00:00")
    assert len(episodes.timeline(mem.conn, ticket_ref="TKT-9")) == 1
    assert len(episodes.timeline(mem.conn, client="KSA")) == 1
    assert len(episodes.timeline(mem.conn, since="2026-11-02T00:00:00+00:00")) == 1
    mem.close()


def test_episode_redaction(tmp_home, clock):
    mem = MemoryDB(clock=clock)
    episodes.append(mem, request_ref="REQ-1", kind="note", summary="contact principal@example.org now")
    assert "principal@example.org" not in episodes.timeline(mem.conn, request_ref="REQ-1")[0]["summary"]
    mem.close()


def test_from_state_db_is_idempotent(tmp_home, clock):
    import sqlite3
    st = tmp_home / "state.db"
    con = sqlite3.connect(str(st))
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, request_id TEXT, actor TEXT, detail_json TEXT)")
    con.executemany("INSERT INTO events (ts, kind, request_id, actor, detail_json) VALUES (?,?,?,?,?)",
                    [("2026-11-01T08:00:00+00:00", "request_opened", "REQ-1", "system", "{}"),
                     ("2026-11-01T09:00:00+00:00", "card_sent", "REQ-1", "bot", '{"kind": "new_request"}')])
    con.commit()
    con.close()
    mem = MemoryDB(clock=clock)
    assert episodes.from_state_db(mem, st) == 2
    assert episodes.from_state_db(mem, st) == 0          # idempotent: re-run adds nothing
    tl = episodes.timeline(mem.conn, request_ref="REQ-1")
    assert [e["kind"] for e in tl] == ["request_opened", "card_sent"]
    mem.close()


def test_from_state_db_missing_is_zero(tmp_home, clock):
    mem = MemoryDB(clock=clock)
    assert episodes.from_state_db(mem, tmp_home / "nope.db") == 0
    mem.close()
