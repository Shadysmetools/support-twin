"""Teamhub intake (Principal 2026-11-15: "only send me what is sent to me").

Only `history search --to-me` results reach him: @mentions of him or his bot, @room, and DMs he did not send.
Room traffic never does. One card per new root or matched reply, today (Cairo) only, never twice, no ticket, no
reply, nothing marked read."""
import asyncio
import json
from datetime import datetime

import pytest

from twin_agent.channels import teamhub_in
from twin_agent.channels.teamhub_in import TeamhubPoller, TeamhubError
from twin_agent.clock import CAIRO
from twin_agent.db import StateDB


def root(mid, text, *, room="AV humans x bots", sender="Hank Sam Arslan", at="2026-11-15 09:10:00.1+00",
         matched=True, replies=()):
    return {"rootMessageId": mid, "roomId": "r1", "roomName": room, "roomKind": "topic", "rootMatched": matched,
            "rootContent": text, "rootSnippet": text[:150], "rootSenderProfile": {"name": sender},
            "rootSentAt": at, "matchedReplyCount": len(replies), "replyCount": len(replies),
            "matchedReplies": [{"messageId": r, "threadSeq": i + 1, "snippet": f"reply {r}"}
                               for i, r in enumerate(replies)]}


def out(*results):
    return json.dumps({"ok": True, "kind": "ok", "text": "", "details": {"results": list(results)}})


EMPTY = json.dumps({"ok": True, "kind": "ok", "text": "No messages matched the given filters.", "details": {}})


@pytest.fixture
def kcfg(cfg):
    cfg["teamhub"] = {"enabled": True, "workspace": "a0000000000a", "cli": "teamhub", "poll_seconds": 60,
                    "notify": True}
    return cfg


@pytest.fixture
def db(tmp_home, kcfg, clock):
    d = StateDB(tmp_home / "data" / "state" / "state.db", clock, kcfg)
    yield d
    d.close()


def make(kcfg, db, clock, tmp_path, outputs):
    calls, toasts = [], []

    def run(args):
        calls.append(args)
        return outputs.pop(0) if outputs else EMPTY
    p = TeamhubPoller(kcfg, db, clock, run=run, notify=lambda title, body: toasts.append((title, body)),
                    inbox_dir=tmp_path)
    return p, calls, toasts


def cards(tmp_path):
    f = tmp_path / "claude_inbox.jsonl"
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.is_file() else []


def test_asks_only_for_what_is_sent_to_principal_since_cairo_midnight(kcfg, db, clock, tmp_path):
    clock.set(datetime(2026, 11, 15, 14, 30, tzinfo=CAIRO))
    p, calls, _ = make(kcfg, db, clock, tmp_path, [EMPTY])
    p.poll_once()
    args = calls[0]
    assert args[:3] == ["workspace", "history", "search"]
    assert "--to-me" in args and "--json" in args
    assert args[args.index("--room") + 1] == "all"
    assert args[args.index("--scope-workspace") + 1] == "a0000000000a"
    assert args[args.index("--since") + 1] == "2026-11-14T22:00:00Z"      # 00:00 Cairo = 22:00 UTC the day before
    assert "timeline" not in args and "read" not in args and "msg" not in args


def test_one_card_and_one_toast_per_new_message(kcfg, db, clock, tmp_path):
    p, _, toasts = make(kcfg, db, clock, tmp_path,
                        [out(root("aa1", "@Principal Doe please review the org tree", room="Structure and Vision",
                                  sender="Hank's Claude"))])
    new = p.poll_once()
    assert [n["message_id"] for n in new] == ["aa1"]
    [c] = cards(tmp_path)
    assert c["type"] == "card" and c["kind"] == "teamhub" and c["source"] == "teamhub"
    assert c["buttons"] == [] and c["request_id"] is None
    assert c["chat"] == "Teamhub: Structure and Vision"
    assert c["root"] == "aa1" and c["message_id"] == "aa1"
    assert "Hank's Claude" in c["text"] and "please review the org tree" in c["text"]
    assert "thread get --root aa1" in c["text"]
    [(title, body)] = toasts
    assert "Teamhub" in title and "Hank's Claude" in title and "org tree" in body


def test_never_twice_even_after_a_restart(kcfg, db, clock, tmp_path):
    same = out(root("aa1", "@Principal Doe hi"))
    p, _, toasts = make(kcfg, db, clock, tmp_path, [same, same])
    assert len(p.poll_once()) == 1
    assert p.poll_once() == []
    p2, _, toasts2 = make(kcfg, db, clock, tmp_path, [same])        # a new listener, same state.db
    assert p2.poll_once() == []
    assert len(cards(tmp_path)) == 1 and len(toasts) == 1 and toasts2 == []


def test_a_new_matched_reply_in_a_known_thread_is_a_new_card(kcfg, db, clock, tmp_path):
    p, _, _ = make(kcfg, db, clock, tmp_path,
                   [out(root("aa1", "@Principal Doe first")),
                    out(root("aa1", "@Principal Doe first", replies=["bb2"]))])
    p.poll_once()
    [n] = p.poll_once()
    assert n["message_id"] == "bb2" and n["root"] == "aa1"
    assert "reply bb2" in cards(tmp_path)[-1]["text"]


def test_root_that_did_not_match_is_not_brought(kcfg, db, clock, tmp_path):
    """A thread found only through a reply that mentions him: the reply comes, not the root's own text."""
    p, _, _ = make(kcfg, db, clock, tmp_path, [out(root("aa1", "status post", matched=False, replies=["bb2"]))])
    assert [n["message_id"] for n in p.poll_once()] == ["bb2"]


def test_older_than_today_is_skipped(kcfg, db, clock, tmp_path):
    clock.set(datetime(2026, 11, 15, 10, 0, tzinfo=CAIRO))
    p, _, toasts = make(kcfg, db, clock, tmp_path, [out(root("old", "@room hi", at="2026-11-14 18:49:39+00"))])
    assert p.poll_once() == [] and toasts == []


def test_seen_list_keeps_only_today(kcfg, db, clock, tmp_path):
    clock.set(datetime(2026, 11, 15, 10, 0, tzinfo=CAIRO))
    p, _, _ = make(kcfg, db, clock, tmp_path, [out(root("aa1", "hi", at="2026-11-15 07:00:00+00"))])
    p.poll_once()
    clock.set(datetime(2026, 11, 16, 10, 0, tzinfo=CAIRO))
    p.run_cli = lambda args: EMPTY
    p.poll_once()
    assert json.loads(db.get_cursor(teamhub_in.CURSOR))["day"] == "2026-11-16"
    assert json.loads(db.get_cursor(teamhub_in.CURSOR))["seen"] == []


def test_cli_error_raises_and_writes_nothing(kcfg, db, clock, tmp_path):
    bad = json.dumps({"ok": False, "kind": "auth_expired", "text": "sign in again"})
    p, _, toasts = make(kcfg, db, clock, tmp_path, [bad])
    with pytest.raises(TeamhubError) as e:
        p.poll_once()
    assert "auth_expired" in str(e.value)
    assert cards(tmp_path) == [] and toasts == []
    p2, _, _ = make(kcfg, db, clock, tmp_path, ["not json"])
    with pytest.raises(TeamhubError):
        p2.poll_once()


def test_a_failed_toast_still_leaves_the_card(kcfg, db, clock, tmp_path):
    def boom(title, body):
        raise OSError("no toast")
    p = TeamhubPoller(kcfg, db, clock, run=lambda a: out(root("aa1", "@Principal Doe hi")), notify=boom,
                    inbox_dir=tmp_path)
    assert len(p.poll_once()) == 1 and len(cards(tmp_path)) == 1


def test_notify_off_means_card_only(kcfg, db, clock, tmp_path):
    kcfg["teamhub"]["notify"] = False
    p, _, toasts = make(kcfg, db, clock, tmp_path, [out(root("aa1", "@Principal Doe hi"))])
    p.poll_once()
    assert toasts == [] and len(cards(tmp_path)) == 1


def test_run_loop_reports_errors_and_keeps_going(kcfg, db, clock, tmp_path):
    kcfg["teamhub"]["poll_seconds"] = 0
    errors, oks = [], []
    seq = [json.dumps({"ok": False, "kind": "network"}), out(root("aa1", "@Principal Doe hi"))]
    p = TeamhubPoller(kcfg, db, clock, run=lambda a: seq.pop(0) if seq else EMPTY, notify=lambda t, b: None,
                    inbox_dir=tmp_path, on_error=errors.append, on_ok=lambda: oks.append(1))

    async def go():
        task = asyncio.create_task(p.run())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if oks:
                break
        p.stop()
        await asyncio.wait_for(task, 2)
    asyncio.run(go())
    assert errors and "network" in errors[0]
    assert oks and len(cards(tmp_path)) == 1


def test_enabled_reads_config(cfg):
    assert teamhub_in.enabled(cfg) is False
    cfg["teamhub"] = {"enabled": True, "workspace": "w1"}
    assert teamhub_in.enabled(cfg) is True
    cfg["teamhub"] = {"enabled": True}
    assert teamhub_in.enabled(cfg) is False                       # no workspace: never poll some default
