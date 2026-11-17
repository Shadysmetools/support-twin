"""Per-person Odoo credentials: each action runs AS the owner, intake as automation, identity checked, missing key
blocks (never a silent fallback), every action audited, the guard holds for every person. FAKE creds only."""
from __future__ import annotations

import json

import pytest

from twin_agent import creds, paths
from twin_agent.helpdesk import guard
from twin_agent.hub import fakes
from twin_agent.hub import store as store_mod
from twin_agent.hub.odoo_gateway import GuardedOdoo, mail_suppress_context
from twin_agent.hub.odoo_people import (DirSecretsBackend, IdentityMismatch, KeyMissing, PeopleConfig, PersonOdoo,
                                         open_person_transport)
from twin_agent.hub.store import MemoryStore
from twin_agent.testing.fake_odoo import FakeOdoo, _seed
from hub_helpers import build_hub, deliver

FAKE_KEY = "FAKE-not-a-key"
UIDS = {"odoo-principal": 777, "odoo-supervisor": 778, "odoo-automation": 900}
PEOPLE = {777: "odoo-principal", 778: "odoo-supervisor"}


class Rig:
    """A shared fake Odoo database + one FakeOdoo transport per secret name, created only while its key is 'present'."""

    def __init__(self, present=("odoo-principal", "odoo-supervisor", "odoo-automation"), uids=None, **cfg):
        self.data = _seed()
        self.present = set(present)
        self.uids = dict(uids or UIDS)
        self.opened: dict[str, FakeOdoo] = {}
        self.store = MemoryStore()
        conv = GuardedOdoo(FakeOdoo(data=self.data), writes_on=True).conv
        pc = PeopleConfig(people=dict(PEOPLE), automation_secret="odoo-automation", automation_uid=900,
                          tg_people={1003: 778}, **cfg)
        self.person = PersonOdoo(config=pc, open_transport=self._open, conv=conv, store=self.store, writes_on=True)

    def _open(self, name):
        if name not in self.present:
            raise KeyMissing(f"no Odoo key stored for {name}")
        t = FakeOdoo(uid=self.uids[name], data=self.data)
        self.opened[name] = t
        return t

    def hub(self, **kw):
        return build_hub(odoo=self.person, store=self.store, **kw)

    def rows(self):
        return store_mod.audit_rows(self.store)

    def authors(self):
        return {m["author_uid"] for m in self.data["mail.message"].values()}


def test_owner_acts_as_self_and_intake_acts_as_automation():
    rig = Rig()
    hub = rig.hub()
    deliver(hub, "webhook_text.json")
    by = {(r["method"], r["acting"]) for r in rig.rows() if r["outcome"] == "ok"}
    assert ("create", "odoo-automation") in by and ("write", "odoo-automation") in by      # intake
    assert ("message_post", "odoo-supervisor") in by                                             # owner's note (Inventory -> Supervisor)
    assert rig.authors() == {778}
    assert rig.opened["odoo-automation"].writes and all(w["method"] != "message_post"
                                                         for w in rig.opened["odoo-automation"].writes)


def test_uid_mismatch_is_refused_before_any_write_and_not_cached():
    rig = Rig(uids={**UIDS, "odoo-supervisor": 777})                 # supervisor's key logs in as someone else
    with pytest.raises(IdentityMismatch):
        rig.person.post_note(9001, "hello", owner_uid=778)
    assert rig.opened["odoo-supervisor"].writes == []
    assert any(r["outcome"] == "refused" and "identity_mismatch" in r["reason"] for r in rig.rows())
    rig.uids["odoo-supervisor"] = 778                                # fixed later: picked up, nothing was cached
    rig.person.post_note(9001, "hello", owner_uid=778)
    assert rig.authors() == {778}


def test_automation_uid_mismatch_is_refused_too():
    rig = Rig(uids={**UIDS, "odoo-automation": 778})
    with pytest.raises(IdentityMismatch):
        rig.person.create_ticket({"name": "x"}, context=mail_suppress_context())
    assert rig.opened["odoo-automation"].writes == []


def test_missing_automation_key_blocks_intake_then_retry_resumes_once_the_key_appears():
    rig = Rig(present=("odoo-principal", "odoo-supervisor"))
    hub = rig.hub()
    out = deliver(hub, "webhook_text.json")
    assert out.get("error") == "KeyMissing" and out.get("retryable") is True
    assert not rig.data["helpdesk.ticket"].get(20001) and any(r["outcome"] == "blocked" for r in rig.rows())
    rig.present.add("odoo-automation")
    hub.retry_failed()
    assert rig.data["helpdesk.ticket"][20001]["user_id"] == 778


def test_missing_owner_key_parks_the_note_and_tick_replays_it_after_the_key_appears():
    rig = Rig(present=("odoo-principal", "odoo-automation"))
    hub = rig.hub()
    deliver(hub, "webhook_text.json")
    assert rig.data["helpdesk.ticket"][20001]["user_id"] == 778        # intake still worked (automation)
    assert 778 not in rig.authors() and any(r["outcome"] == "blocked" for r in rig.rows())
    assert hub.tick().get("notes_replayed") == 0
    rig.present.add("odoo-supervisor")
    assert hub.tick()["notes_replayed"] >= 1
    assert 778 in rig.authors()
    assert hub.tick()["notes_replayed"] == 0                           # each parked note is replayed once


def test_no_fallback_by_default_and_explicit_fallback_is_audited():
    rig = Rig(present=("odoo-automation",))
    with pytest.raises(KeyMissing):
        rig.person.post_note(9001, "n", owner_uid=778)
    assert rig.authors() == set()
    rig = Rig(present=("odoo-automation",), fallback_to_automation=True)
    rig.person.post_note(9001, "n", owner_uid=778)
    assert rig.authors() == {900}
    row = [r for r in rig.rows() if r["outcome"] == "ok"][-1]
    assert (row["acting"], row["on_behalf_of"], row["reason"]) == ("odoo-automation", "odoo-supervisor", "no_key")


def test_unmapped_owner_is_blocked_not_guessed():
    rig = Rig()
    with pytest.raises(KeyMissing):
        rig.person.post_note(9001, "n", owner_uid=555)


def test_guard_holds_for_a_person_transport():
    rig = Rig()
    gw, _ = rig.person._gateway(778)
    assert gw.conv is rig.person.conv and gw.adapter.writes_on is True
    with pytest.raises(guard.WriteRefused):
        gw.adapter.call("helpdesk", "helpdesk.ticket", "unlink", [[9001]])
    with pytest.raises(guard.WriteRefused):
        gw.adapter.call("helpdesk", "res.partner", "write", [[3001], {"name": "x"}])
    with pytest.raises(guard.WriteRefused):
        gw.create_ticket({"name": "x", "stage_id": 14}, context={"tracking_disable": True})     # mail-suppress missing
    with pytest.raises(guard.WriteRefused):                                                      # no comment to anyone
        gw.adapter.call("helpdesk", "helpdesk.ticket", "message_post", [[9001]],
                        {"body": "x", "subtype_xmlid": "mail.mt_comment", "partner_ids": [1]})
    assert rig.opened["odoo-supervisor"].writes == []


def test_writes_gate_is_shared_by_every_person():
    rig = Rig()
    rig.person.writes_on = False
    rig.person._cache.clear()
    with pytest.raises(guard.WriteRefused):
        rig.person.post_note(9001, "n", owner_uid=778)
    assert rig.opened["odoo-supervisor"].writes == []
    assert any(r["outcome"] == "refused" for r in rig.rows())


def test_reassign_acts_as_the_tapper_and_audits_old_and_new_owner():
    rig = Rig()
    hub = rig.hub()
    deliver(hub, "webhook_text.json")
    res = hub.reassign(20001, fakes.owner(777, "Principal", 1001), actor_tg_id=1003)
    assert res["ok"] is True
    row = [r for r in rig.rows() if r["method"] == "write" and r.get("old_owner_uid")][-1]
    assert (row["acting"], row["old_owner_uid"], row["new_owner_uid"]) == ("odoo-supervisor", 778, 777)


def test_reassign_by_a_tapper_without_a_key_is_blocked_and_changes_nothing():
    rig = Rig(present=("odoo-principal", "odoo-automation"))
    hub = rig.hub()
    deliver(hub, "webhook_text.json")
    res = hub.reassign(20001, fakes.owner(777, "Principal", 1001), actor_tg_id=1003)
    assert res == {"ok": False, "reason": "odoo_no_key"}
    assert rig.data["helpdesk.ticket"][20001]["user_id"] == 778 and hub.ticket(20001).owner.uid == 778


def test_audit_rows_never_hold_secrets_phones_or_message_ids():
    rig = Rig(present=("odoo-principal", "odoo-automation"))
    hub = rig.hub()
    deliver(hub, "webhook_text.json")
    blob = json.dumps(rig.rows())
    assert rig.rows() and "201000000001" not in blob and "wamid" not in blob and FAKE_KEY not in blob
    assert {"ts", "acting", "on_behalf_of", "model", "method", "ticket_id", "outcome", "reason"} <= set(rig.rows()[0])


# ---------------------------------------------------------------- config + the real transport opener (fake secrets)

def test_config_maps_by_uid_normalises_yaml_keys_and_requires_automation():
    cfg = {"hub": {"odoo_people": {"778": "odoo-supervisor"}, "odoo_automation": "odoo-riley", "odoo_automation_uid": "900",
                   "odoo_tg_people": {"1003": "778"}}}
    pc = PeopleConfig.from_cfg(cfg)
    assert pc.people == {778: "odoo-supervisor"} and pc.automation_secret == "odoo-riley" and pc.automation_uid == 900
    assert pc.tg_people == {1003: 778} and pc.fallback_to_automation is False
    with pytest.raises(ValueError):
        PeopleConfig.from_cfg({"hub": {"odoo_automation": "odoo-riley"}})


def test_open_person_transport_reads_the_secrets_folder_and_never_leaks(tmp_home, tmp_path):
    (tmp_path / "odoo-supervisor.json").write_text(json.dumps({"login": "fake@example.org", "api_key": FAKE_KEY}))
    (tmp_path / "odoo-riley.json").write_text("not json")
    be = DirSecretsBackend(tmp_path)
    cfg = {"odoo_sys": {"url": "https://odoo.example.test", "db": "fakedb"}}
    t = open_person_transport(cfg, "odoo-supervisor", backend=be)
    assert FAKE_KEY not in repr(t) and t.uid == 0                      # lazy: nothing connected
    with pytest.raises(KeyMissing):
        open_person_transport(cfg, "odoo-principal", backend=be)           # absent
    with pytest.raises(KeyMissing, match="unreadable"):
        open_person_transport(cfg, "odoo-riley", backend=be)            # malformed
    with pytest.raises(KeyMissing):
        open_person_transport(cfg, "not-a-vault-name", backend=be)     # unknown name fails closed
    log = paths.logs_dir() / "vault-audit.jsonl"
    assert "odoo-supervisor" in creds.NAMES and FAKE_KEY not in (log.read_text(encoding="utf-8") if log.exists() else "")


# ---------------------------------------------------------------- M2 / L2 / L3

def test_reassign_by_an_unmapped_tapper_is_blocked_with_an_audit_row():
    rig = Rig()
    hub = rig.hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "approver_tg_ids": [2002]})
    deliver(hub, "webhook_text.json")
    res = hub.reassign(20001, fakes.owner(777, "Principal", 1001), actor_tg_id=2002)
    assert res == {"ok": False, "reason": "odoo_tapper_unmapped"} and hub.ticket(20001).owner.uid == 778
    row = [r for r in rig.rows() if r["reason"] == "tapper_unmapped"][-1]
    assert row["outcome"] == "blocked" and row["on_behalf_of"].startswith("tg:") and "2002" not in json.dumps(rig.rows())


def test_unmapped_tapper_with_the_explicit_fallback_acts_as_automation_and_is_audited():
    rig = Rig(fallback_to_automation=True)
    hub = rig.hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "approver_tg_ids": [2002]})
    deliver(hub, "webhook_text.json")
    assert hub.reassign(20001, fakes.owner(777, "Principal", 1001), actor_tg_id=2002)["ok"] is True
    row = [r for r in rig.rows() if r.get("old_owner_uid")][-1]
    assert (row["acting"], row["role"], row["reason"], row["outcome"]) == ("odoo-automation", "automation",
                                                                         "tapper_unmapped", "ok")
    assert row["on_behalf_of"].startswith("tg:") and (row["old_owner_uid"], row["new_owner_uid"]) == (778, 777)


def test_audit_rows_carry_a_role_so_the_interim_automation_account_is_visible():
    rig = Rig()
    rig.person.post_note(9001, "n", owner_uid=778)
    rig.person.create_ticket({"name": "x", "stage_id": 14}, context=mail_suppress_context())
    assert {(r["acting"], r["role"]) for r in rig.rows()} == {("odoo-supervisor", "person"), ("odoo-automation", "automation")}


def test_parked_note_replays_as_the_CURRENT_owner_and_its_text_is_blanked():
    rig = Rig(present=("odoo-principal", "odoo-automation"))
    hub = rig.hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "approver_tg_ids": [1001]})
    deliver(hub, "webhook_text.json")                             # owner Supervisor has no key: her note is parked
    assert any(d.get("done") is False and d["body"] for d in rig.store.scan("pending_notes"))
    rig.person.cfg.tg_people[1001] = 777
    hub.reassign(20001, fakes.owner(777, "Principal", 1001), actor_tg_id=1001)    # the ticket moves to Principal (has a key)
    rig.present.add("odoo-supervisor")
    hub.tick()
    assert 777 in rig.authors() and 778 not in rig.authors()
    assert all(d["body"] == "" for d in rig.store.scan("pending_notes") if d["done"] is True)


def test_old_parked_notes_expire_and_lose_their_text():
    from datetime import datetime, timedelta, timezone
    rig = Rig(present=("odoo-automation",))
    with pytest.raises(KeyMissing):
        rig.person.post_note(9001, "client words", owner_uid=778)
    [(k, d)] = [(d["_key"], d) for d in rig.store.scan("pending_notes")]
    rig.store.put("pending_notes", k, {**d, "ts": (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()})
    assert rig.person.replay_notes() == 0
    assert rig.store.get("pending_notes", k)["body"] == "" and rig.store.get("pending_notes", k)["done"] == "expired"


def test_real_ticket_writer_redacts_names_and_phones_before_any_call():
    from twin_agent.hub.adapters_real import RealTicketWriter
    out = RealTicketWriter.prepare("Hi I am Oscar Client, call +201001234567 about stock", "Nile Trading Co", ["Oscar Client"])
    assert "Oscar Client" not in out and "+201001234567" not in out and "[NAME]" in out


# ---------------------------------------------------------------- review round 2: replay fairness, at-most-once

def _park(rig, n, owner_uid):
    for i in range(n):
        try:
            rig.person.post_note(9001, f"note {owner_uid}-{i}", owner_uid=owner_uid)
        except KeyMissing:
            pass


def test_M2_a_person_without_a_key_does_not_starve_others_or_flood_the_audit():
    rig = Rig(present=("odoo-automation",))                               # neither Supervisor nor Principal has a key yet
    _park(rig, 60, 778)
    _park(rig, 1, 777)                                                    # Principal's note is parked AFTER Supervisor's 60
    rig.present.add("odoo-principal")                                         # ... and only Principal's key shows up
    rows_before = len(rig.rows())
    posted = [rig.person.replay_notes() for _ in range(5)]
    assert posted[0] == 1 and 777 in rig.authors()                        # Principal's note went out on the FIRST pass
    assert len(rig.rows()) - rows_before <= 2                             # 5 passes: ok row + at most ONE blocked row/hour


def test_M3_a_parked_note_is_posted_at_most_once_even_if_the_reply_is_lost():
    rig = Rig(present=("odoo-automation",))
    _park(rig, 1, 778)
    rig.present.add("odoo-supervisor")
    real = rig.person._open

    def flaky(name):
        t = real(name)
        orig = t.execute_kw

        def ek(model, method, args, kw):
            out = orig(model, method, args, kw)
            if method == "message_post":
                raise TimeoutError("lost the reply after Odoo accepted it")
            return out
        t.execute_kw = ek
        return t
    rig.person._open = flaky
    rig.person.replay_notes()
    rig.person.replay_notes()
    rig.person.replay_notes()
    posts = [m for m in rig.data["mail.message"].values()]
    assert len(posts) == 1
    [d] = rig.store.scan("pending_notes")
    assert d["done"] == "unknown" and d["body"] == ""


def test_M3_a_row_left_posting_by_a_crash_is_never_re_posted():
    rig = Rig()
    _park(rig, 0, 778)
    rig.store.put("pending_notes", "k1", {"_key": "k1", "tid": 9001, "body": "x", "owner_uid": 778, "done": False,
                                          "state": "posting", "ts": "2025-11-11T12:00:00+00:00"})
    assert rig.person.replay_notes() == 0 and rig.data["mail.message"] == {}
    assert rig.store.get("pending_notes", "k1")["done"] == "unknown"


def test_minor_a_note_blocked_by_identity_is_parked_not_dropped():
    rig = Rig(uids={**UIDS, "odoo-supervisor": 777})
    with pytest.raises(IdentityMismatch):
        rig.person.post_note(9001, "keep me", owner_uid=778)
    assert [d["body"] for d in rig.store.scan("pending_notes")] == ["keep me"]
    rig.uids["odoo-supervisor"] = 778
    assert rig.person.replay_notes() == 1 and 778 in rig.authors()
