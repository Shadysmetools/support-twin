"""Tests for board/server.py (BOARD): read-only, Host check, URL token, redaction (red-team #22)."""
from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from twin_agent.board import server as board

TOKEN = "test-token-0123456789abcdef"

SCHEMA = """
CREATE TABLE requests (id TEXT PRIMARY KEY, state TEXT NOT NULL, prev_state TEXT, lane TEXT, tier TEXT, company TEXT,
  channel TEXT NOT NULL, chat_id TEXT, chat TEXT, topic_id INTEGER, topic TEXT, root_msg_id TEXT, requester_id TEXT,
  requester_name TEXT, title TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, burst_closed_at TEXT,
  first_reply_at TEXT, first_reply_by TEXT, taken_over INTEGER DEFAULT 0, no_holding INTEGER DEFAULT 0,
  fully_blocked INTEGER DEFAULT 0, ticket_url TEXT);
CREATE TABLE timers (id INTEGER PRIMARY KEY, request_id TEXT, kind TEXT NOT NULL, due_at TEXT NOT NULL,
  fired_at TEXT, cancelled_at TEXT, payload_json TEXT);
CREATE TABLE cards (id INTEGER PRIMARY KEY, request_id TEXT, kind TEXT, to_user TEXT, text TEXT, buttons_json TEXT,
  silent INTEGER, bot_msg_id INTEGER, created_at TEXT, sent_at TEXT, expires_at TEXT, coalesced_into INTEGER);
CREATE TABLE sends (send_key TEXT PRIMARY KEY, request_id TEXT, channel TEXT, kind TEXT, lane TEXT, trigger TEXT,
  target_json TEXT, text_hash TEXT, status TEXT, created_at TEXT, sent_at TEXT, remote_id TEXT, from_id_ok INTEGER,
  guard_json TEXT, error TEXT);
CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, request_id TEXT, actor TEXT, detail_json TEXT);
CREATE TABLE kpis (id INTEGER PRIMARY KEY, day TEXT, lane TEXT, name TEXT, value REAL, request_id TEXT,
  detail_json TEXT);
"""


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
def db_path(tmp_home):
    now = datetime.now(timezone.utc)
    p = tmp_home / "data" / "state" / "state.db"
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO requests (id, state, channel, chat, requester_name, title, created_at, updated_at) "
                "VALUES ('REQ-0001','TRIAGED','telegram_group','Test Group','<script>x</script>','t',?,?)",
                (iso(now - timedelta(minutes=20)), iso(now)))
    con.execute("INSERT INTO requests (id, state, channel, created_at, updated_at, first_reply_at) "
                "VALUES ('REQ-0002','CLOSED_PROVEN','telegram_group',?,?,?)",
                (iso(now - timedelta(days=1)), iso(now), iso(now - timedelta(days=1) + timedelta(minutes=12))))
    con.execute("INSERT INTO timers (request_id, kind, due_at) VALUES ('REQ-0001','holding',?)",
                (iso(now + timedelta(minutes=40)),))
    con.execute("INSERT INTO cards (request_id, kind, to_user, text, created_at) VALUES "
                "('REQ-0001','new_request','principal','see https://helpdesk.example.com/my/ticket/12/abcdef123456 call 01012345678',?)",
                (iso(now),))
    con.execute("INSERT INTO sends (send_key, request_id, status, created_at) VALUES ('REQ-0001:holding','REQ-0001',"
                "'would_send',?)", (iso(now),))
    con.execute("INSERT INTO kpis (day, lane, name, value) VALUES ('2026-11-01', 'L1_holding', 'shadow_agreement', 0.8)")
    con.commit()
    con.close()
    (tmp_home / "data" / "logs" / "actions.jsonl").write_text(
        json.dumps({"ts": iso(now), "action": "dial", "detail": "token=supersecretvalue1"}) + "\n", encoding="utf-8")
    return p


@pytest.fixture
def running(db_path):
    srv = board.make_server(db_path, port=0, token=TOKEN)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def get(port, path, host=None, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest(method, path, skip_host=True)
    c.putheader("Host", host if host is not None else f"127.0.0.1:{port}")
    c.endheaders()
    r = c.getresponse()
    body = r.read().decode("utf-8")
    c.close()
    return r.status, body


def test_page_renders_redacted_and_escaped(running):
    status, body = get(running, f"/?t={TOKEN}")
    assert status == 200
    assert "REQ-0001" in body and "REQ-0002" not in body.split("Live requests")[1].split("Timers")[0]
    assert "abcdef123456" not in body and "01012345678" not in body and "supersecretvalue1" not in body
    assert "<script>x" not in body and "&lt;script&gt;" in body


def test_api_parts(running):
    status, body = get(running, f"/api/kpis?t={TOKEN}")
    assert status == 200
    k = json.loads(body)
    assert k["open_requests"] == 1 and k["waiting_first_reply"] == 1
    assert k["first_reply_median_min_7d"] == 12.0
    assert k["sends_7d"] == {"would_send": 1}
    assert k["stored"][0]["name"] == "shadow_agreement"
    timers = json.loads(get(running, f"/api/timers?t={TOKEN}")[1])
    assert timers[0]["kind"] == "holding"
    audit = json.loads(get(running, f"/api/audit?t={TOKEN}")[1])
    assert "supersecretvalue1" not in json.dumps(audit)


@pytest.mark.parametrize("path,host", [
    ("/", None),                                   # no token
    ("/?t=wrong-token-000000000000", None),
    (f"/?t={TOKEN}", "localhost:{port}"),          # DNS rebinding style host
    (f"/?t={TOKEN}", "evil.example.com"),
    (f"/?t={TOKEN}", ""),
])
def test_forbidden(running, path, host):
    h = host.format(port=running) if host is not None else None
    assert get(running, path, host=h)[0] == 403


def test_no_actions(running):
    assert get(running, f"/?t={TOKEN}", method="POST")[0] == 405
    assert get(running, f"/?t={TOKEN}", method="DELETE")[0] == 405
    assert get(running, f"/api/stop?t={TOKEN}")[0] == 404


def test_db_not_modified_and_missing_db_ok(tmp_home, db_path):
    before = db_path.read_bytes()
    data = board.collect(db_path, tmp_home / "data" / "logs", datetime.now(timezone.utc))
    assert db_path.read_bytes() == before and data["requests"]
    empty = board.collect(tmp_home / "nope.db", tmp_home / "nope", datetime.now(timezone.utc))
    assert empty["requests"] == [] and empty["kpis"]["open_requests"] == 0
    assert "none" in board.render_html(empty, TOKEN)


def test_refuses_non_loopback_and_short_token(db_path):
    with pytest.raises(ValueError):
        board.make_server(db_path, host="0.0.0.0", port=0, token=TOKEN)
    with pytest.raises(ValueError):
        board.make_server(db_path, port=0, token="short")
