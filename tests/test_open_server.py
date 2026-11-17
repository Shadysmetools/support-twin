"""The "Open as a session in Claude Code" link (Principal 2026-11-12). The email links to the twin's own server on
127.0.0.1, because Gmail and Odoo's sanitizer may drop a custom claude:// href. The server answers with a page that
opens claude://code/new?q=<prompt>&folder=<hub> for a ticket the twin routed, and nothing else."""
import json
import threading
import urllib.error
import urllib.request
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from twin_agent.db import StateDB
from twin_agent.helpdesk import open_server
from twin_agent.testing.fakes import STAFF_A, make_event


@pytest.fixture
def routed(tmp_home, cfg, clock):
    db = StateDB(tmp_home / "data" / "state" / "state.db", clock, cfg)
    q = make_event("BATCH/13961 need to validate", msg_id=1, sender=STAFF_A, ts=clock.now())
    rid = db.create_request(q, requester_id="2001", requester_name="Max Alaa", lane="L4_t2a_fix_proposal",
                            company="EG", title="BATCH/13961")
    db.add_message(q, rid)
    db.update_request(rid, ticket_id="2690")
    db.save_packet(rid, {"request_id": rid, "tier": "T2a", "analysis": "Negative stock on Cars/Mall of Egypt.",
                         "draft_reply": {"text": "Please return the Sunken Night Stand first."}}, True, [])
    db.log_event("ticket_routed", rid, actor="helpdesk", key="k", ticket=2690, track="functional", type="bug",
                 confidence=0.82, level="GREEN", stage=2, source="worker", reasons=[])
    yield db, rid
    db.close()


def _serve(db_path):
    srv = open_server.make_server(db_path, host="127.0.0.1", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _get(url, host=None):
    req = urllib.request.Request(url, headers={"Host": host} if host else {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.read().decode("utf-8")


def test_open_link_gives_the_claude_code_deep_link(routed, tmp_home):
    db, rid = routed
    srv, base = _serve(tmp_home / "data" / "state" / "state.db")
    try:
        status, page = _get(f"{base}/open/2690")
    finally:
        srv.shutdown()
    assert status == 200
    link = page.split('href="', 1)[1].split('"', 1)[0].replace("&amp;", "&")
    parts = urlsplit(link)
    assert parts.scheme == "claude" and parts.netloc == "code" and parts.path == "/new"
    qs = parse_qs(parts.query)
    assert qs["folder"] == [r"C:\twin\hub"]
    prompt = qs["q"][0]
    assert prompt.startswith("/functional-consultant ticket #2690")
    assert rid in prompt and "BATCH/13961 need to validate" in prompt and "claude_inbox edit" in prompt
    assert "Negative stock on Cars/Mall of Egypt." in prompt


def test_unknown_or_unrouted_ticket_is_not_opened(routed, tmp_home):
    srv, base = _serve(tmp_home / "data" / "state" / "state.db")
    try:
        for path in ("/open/9999", "/open/abc", "/", "/open/2690/../x"):
            with pytest.raises(urllib.error.HTTPError) as e:
                _get(base + path)
            assert e.value.code in (400, 404)
    finally:
        srv.shutdown()


def test_other_host_header_is_refused(routed, tmp_home):
    """DNS rebinding: a page on another site must not drive this server."""
    srv, base = _serve(tmp_home / "data" / "state" / "state.db")
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(f"{base}/open/2690", host="evil.example")
        assert e.value.code == 403
    finally:
        srv.shutdown()


def test_server_binds_localhost_only():
    with pytest.raises(ValueError):
        open_server.make_server("x.db", host="0.0.0.0", port=0)


def test_prompt_reply_command_runs_without_isolated_mode(routed, tmp_home):
    """`python -I -m twin_agent...` cannot import the package (12 Nov test). The prompt must give a working one."""
    db, rid = routed
    link = open_server.build_link(tmp_home / "data" / "state" / "state.db", 2690)
    prompt = parse_qs(urlsplit(link).query)["q"][0]
    assert " -I -m " not in prompt
    assert f'-m twin_agent.channels.claude_inbox edit "<reply>" {rid}' in prompt
    assert "run in C:\\twin\\support-twin" in prompt
