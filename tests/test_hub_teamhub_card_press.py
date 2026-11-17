"""Card presses read back from the card message: a fake Teamhub CLI returns canned ``message get`` JSON. No network."""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from twin_agent.hub import teamhub_card_press as cp
from twin_agent.hub import odoo_ticket_watch as w
from twin_agent.hub.teamhub_card_press import NS, scan_card_presses
from twin_agent.hub.teamhub_threads import process_all
from teamhub_helpers import EXECUTIVE, SYSADMIN, make_ctx, ticket


class FakeCardCli:
    """Stands in for ``teamhub workspace message get``: returns the card with the interactions added so far."""

    def __init__(self, mid: str):
        self.mid, self.by_actor, self.calls, self.fail = mid, {}, [], False

    def press(self, actor, action, value, at="2026-11-16T11:01:55.533Z", **extra):
        self.by_actor[actor] = {"actorId": actor, "actorName": "x", "actionId": action, "label": action, "value": value,
                                "actedAt": at, **extra}
        return self

    def message(self) -> dict:
        return {"id": self.mid, "senderType": "agent", "senderUserId": "agent1", "content": "card",
                "metadata": {"ui": {"actions": [{"actionId": "approve"}], "interactionMode": "scoped_per_user",
                                    "interactionsByActor": self.by_actor, "interactedActions": {}}}}

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"ok": True, "details": {"message": self.message()}}))


def opened(tmp_path, **kw):
    ctx, agent, odoo, clock = make_ctx(tmp_path, [ticket(5)], **kw)
    assert w.watch_once(ctx)["opened"] == [5]
    doc = ctx.approvals.for_ticket(5)
    cli = FakeCardCli(doc["card_message_id"])
    ctx.cli_runner = cli
    return ctx, agent, clock, doc, cli


def texts(agent):
    return [o["text"] for o in agent.of("reply")]


def test_approve_press_is_decided_with_the_platform_actor(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.press(SYSADMIN, "approve", doc["nonce"])
    out = scan_card_presses(ctx)
    d = ctx.approvals.get(doc["nonce"])
    assert out[0]["reason"] is None and out[0]["ticket_id"] == 5
    assert d["decision"] == "approved" and d["used_by"] == SYSADMIN and d["identity"] == "teamhub_card_actor"
    assert ctx.threads.get(5)["decision"]["identity"] == "teamhub_card_actor"
    assert "Approved by Sysadmin" in texts(agent)[-1] and "nothing was sent to the client" in texts(agent)[-1]
    assert cli.calls[0][1:4] == ["workspace", "message", "get"] and "--scope-room" in cli.calls[0] and "--json" in cli.calls[0]


def test_reject_press(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.press(SYSADMIN, "reject", doc["nonce"])
    scan_card_presses(ctx)
    assert ctx.approvals.get(doc["nonce"])["decision"] == "rejected" and "Rejected by Sysadmin" in texts(agent)[-1]


def test_edit_press_issues_an_edit_card_that_takes_the_next_press(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.press(SYSADMIN, "edit", doc["nonce"])
    scan_card_presses(ctx)
    kids = [x for x in ctx.store.scan("teamhub_approvals") if x.get("parent") == doc["nonce"]]
    assert ctx.approvals.get(doc["nonce"])["decision"] == "edit_requested" and len(kids) == 1 and not ctx.approvals.decided(5)
    assert kids[0]["kind"] == "edit" and kids[0]["card_message_id"] and "Edit requested by Sysadmin" in " ".join(texts(agent))
    assert agent.of("card")[-1]["actions"][0]["actionId"] == "approve_edited"
    new = FakeCardCli(kids[0]["card_message_id"])               # Teamhub shows the NEW card; Sysadmin edits and presses
    ctx.cli_runner = new
    new.press(SYSADMIN, "approve_edited", kids[0]["nonce"], inputValues={"text": "Hello, we are on it today."})
    scan_card_presses(ctx)
    d = ctx.approvals.get(kids[0]["nonce"])
    assert d["decision"] == "edited" and d["approved_text"] == "Hello, we are on it today." and ctx.approvals.decided(5)
    assert "Edited and approved by Sysadmin" in texts(agent)[-1]


def test_edit_card_press_without_text_is_refused_and_stays_open(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.press(SYSADMIN, "edit", doc["nonce"])
    scan_card_presses(ctx)
    kid = next(x for x in ctx.store.scan("teamhub_approvals") if x.get("parent") == doc["nonce"])
    ctx.cli_runner = FakeCardCli(kid["card_message_id"]).press(SYSADMIN, "approve_edited", kid["nonce"])
    scan_card_presses(ctx)
    assert ctx.approvals.get(kid["nonce"])["used_at"] is None and "refused" in texts(agent)[-1]


def test_wrong_actor_is_refused_silently_and_does_not_burn(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    n = len(agent.ops)
    cli.press(EXECUTIVE, "approve", doc["nonce"])                  # on the allowlist, but not this card's target
    out = scan_card_presses(ctx)
    assert out[0]["reason"] == "not_authorised" and ctx.approvals.get(doc["nonce"])["used_at"] is None
    assert len(agent.ops) == n


def test_wrong_nonce_is_refused(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.press(SYSADMIN, "approve", "0" * 32)
    assert scan_card_presses(ctx)[0]["reason"] == "wrong_card"
    assert ctx.approvals.get(doc["nonce"])["used_at"] is None


def test_unknown_action_is_refused(tmp_path):
    ctx, _, _, doc, cli = opened(tmp_path)
    cli.press(SYSADMIN, "delete", doc["nonce"])
    assert scan_card_presses(ctx)[0]["reason"] == "unknown_action"


def test_expired_press_is_refused_with_a_visible_reply(tmp_path):
    ctx, agent, clock, doc, cli = opened(tmp_path)
    clock.t += timedelta(hours=ctx.st.approval_ttl_hours + 1)
    cli.press(SYSADMIN, "approve", doc["nonce"])
    out = scan_card_presses(ctx)
    assert out[0]["reason"] == "expired" and ctx.approvals.get(doc["nonce"])["decision"] is None
    assert "refused" in texts(agent)[-1]


def test_already_decided_by_a_text_reply_wins(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    ctx.approvals.decide(doc["nonce"], SYSADMIN, "reject", None, doc["card_message_id"], "teamhub_message_author", "r1")
    cli.press(SYSADMIN, "approve", doc["nonce"])
    assert scan_card_presses(ctx) == []                          # no unused approval: the card is not even read
    assert ctx.approvals.get(doc["nonce"])["decision"] == "rejected" and ctx.store.scan(NS) == []


def test_ticket_decided_between_listing_and_judging_is_refused(tmp_path):
    ctx, _, _, doc, cli = opened(tmp_path)
    ctx.approvals.decide(doc["nonce"], SYSADMIN, "approve", None, doc["card_message_id"], "teamhub_message_author", "r1")
    rec = cp._judge(ctx, ctx.approvals.get(doc["nonce"]), {"actorId": SYSADMIN, "actionId": "reject", "value": doc["nonce"],
                                                           "actedAt": "2026-11-16T11:00:00Z"})
    assert rec["reason"] == "ticket_decided"


def test_the_same_interaction_seen_twice_is_processed_once(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.press(SYSADMIN, "edit", doc["nonce"])
    scan_card_presses(ctx)
    n, cards = len(agent.ops), len(agent.of("card"))
    scan_card_presses(ctx)                                        # the old card still shows the Edit; it is no longer open
    assert len(agent.ops) == n and len(agent.of("card")) == cards and len(ctx.store.scan(NS)) == 1
    rid = cp.press_id(doc["card_message_id"], cp.interactions(cli.message())[0])
    ctx.approvals.revoke(5)                                       # even if the card were open again, the id is already stored
    assert ctx.store.get(NS, rid) is not None


def test_cli_failure_decides_nothing(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.press(SYSADMIN, "approve", doc["nonce"])
    cli.fail = True
    assert scan_card_presses(ctx) == []
    assert ctx.approvals.get(doc["nonce"])["used_at"] is None and ctx.store.scan(NS) == []


def test_malformed_interactions_are_ignored(tmp_path):
    ctx, _, _, doc, cli = opened(tmp_path)
    cli.by_actor = {SYSADMIN: {"actorId": "someone_else", "actionId": "approve", "value": doc["nonce"], "actedAt": "2026-11-16T11:00:00Z"},
                    EXECUTIVE: {"actorId": EXECUTIVE, "actionId": "approve", "value": doc["nonce"], "actedAt": "not a time"}}
    assert scan_card_presses(ctx) == []


def test_dry_run_decides_and_writes_nothing(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    ctx.dry_run = True
    n = len(agent.ops)
    cli.press(SYSADMIN, "approve", doc["nonce"])
    out = scan_card_presses(ctx)
    assert out[0]["reason"] is None and ctx.approvals.get(doc["nonce"])["used_at"] is None
    assert ctx.store.scan(NS) == [] and len(agent.ops) == n


def test_crash_after_the_record_is_finished_by_the_next_scan(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.press(SYSADMIN, "approve", doc["nonce"])
    rec = cp._judge(ctx, doc, cp.interactions(cli.message())[0])
    ctx.store.put_if_absent(NS, cp.press_id_of(rec), rec)        # stored, then the process died before deciding
    assert ctx.approvals.get(doc["nonce"])["used_at"] is None
    scan_card_presses(ctx)
    assert ctx.approvals.get(doc["nonce"])["decision"] == "approved" and "Approved by Sysadmin" in texts(agent)[-1]


def test_process_all_runs_card_presses(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.press(SYSADMIN, "approve", doc["nonce"])
    process_all(ctx)
    assert ctx.approvals.get(doc["nonce"])["decision"] == "approved"
