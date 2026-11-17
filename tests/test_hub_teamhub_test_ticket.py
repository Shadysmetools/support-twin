"""The Teamhub test-ticket tool and its guard allowance. Fakes only: no Odoo, no network."""
from __future__ import annotations

from pathlib import Path

import pytest

from twin_agent.helpdesk import guard
from twin_agent.hub import teamhub_test_ticket as kt

OPEN = {"id": 1, "name": "Open", "template_id": False, "sequence": 1, "is_close": False}
SOLVED = {"id": 3, "name": "Solved", "template_id": False, "sequence": 3, "is_close": True}


class FakeT:
    """Scripted Odoo. ``mail_on_create`` simulates a leaked email; ``swap`` a new mail row with an unchanged count."""

    def __init__(self, stages=None, assign="manual", mail_on_create=0, partner_email=False, with_partner=True,
                 partner_name=guard.TEST_PARTNER, swap=False):
        self.stages = stages if stages is not None else [OPEN, SOLVED]
        self.assign, self.mail_on_create, self.partner_email, self.with_partner = assign, mail_on_create, partner_email, with_partner
        self.partner_name, self.swap = partner_name, swap
        self.mail, self.writes, self.calls, self.uid, self.new_ids = 100, [], [], 82, []
        self.ticket = {"id": 77, "name": "TEST · Sysadmin's Tickets flow", "team_id": [2, "HS"], "stage_id": [1, "Open"],
                       "partner_id": [10, "T"]}

    def _partner(self):
        return {"id": 10, "name": self.partner_name, "email": "a@b.example.org" if self.partner_email else False,
                "phone": False, "mobile": False}

    def execute_kw(self, model, method, args, kwargs):
        self.calls.append((model, method))
        if method == "fields_get":
            return {"is_close": {}} if model == "helpdesk.stage" else {"assign_method": {}}
        if (model, method) == ("mail.mail", "search_count"):
            return self.mail
        if (model, method) == ("mail.mail", "search"):
            return [self.mail] if kwargs.get("limit") else self.new_ids
        if (model, method) == ("mail.message", "search_count"):
            return 0
        if method == "read":
            return [self._partner()] if model == "res.partner" else [self.ticket]
        if model == "helpdesk.stage":
            return self.stages
        if model == "helpdesk.team":
            return [{"id": 2, "assign_method": self.assign}]
        if model == "res.partner" and method == "search_read":
            return [self._partner()] if self.with_partner else []
        if model == "helpdesk.ticket" and method == "search_read":
            return [self.ticket]
        assert kwargs["context"]["mail_notrack"] and kwargs["context"]["tracking_disable"]      # every write is mail-off
        self.writes.append((model, method, args))
        if self.swap:
            self.new_ids = [999]
        else:
            self.mail += self.mail_on_create
            self.new_ids = [self.mail] if self.mail_on_create else []
        return 10 if model == "res.partner" else 77


def test_dry_run_reads_only():
    t, lines = FakeT(), []
    r = kt.create(t, live=False, out=lines.append)
    assert r["dry_run"] and t.writes == [] and r["manual_assignment"]
    assert kt.close(t, 77, live=False, out=lines.append)["dry_run"] and t.writes == []


def test_live_create_makes_a_test_ticket_with_no_owner_and_prints_the_mail_delta():
    t, lines = FakeT(), []
    r = kt.create(t, live=True, out=lines.append)
    model, method, args = t.writes[-1]
    vals = args[0]
    assert (model, method) == ("helpdesk.ticket", "create") and vals["name"].startswith("TEST · ")
    assert "user_id" not in vals and vals["team_id"] == 2 and vals["stage_id"] == 1 and vals["partner_id"] == 10
    assert r["delta"]["mail_mail"] == 0 and any("delta=" in l for l in lines)


def test_a_new_mail_row_stops_the_run():
    with pytest.raises(kt.Refuse, match="mail appeared"):
        kt.create(FakeT(mail_on_create=1), live=True, out=lambda *_: None)


def test_M1_a_new_mail_id_is_caught_even_when_the_count_does_not_change():
    t, lines = FakeT(swap=True), []
    with pytest.raises(kt.Refuse, match="mail appeared"):
        kt.create(t, live=True, out=lines.append)
    assert any("[999]" in l for l in lines)


def test_creates_the_test_contact_with_no_email_when_missing():
    t = FakeT(with_partner=False)
    kt.create(t, live=True, out=lambda *_: None)
    assert t.writes[0] == ("res.partner", "create", [{"name": guard.TEST_PARTNER}])


def test_refuses_a_test_contact_that_has_an_email():
    with pytest.raises(kt.Refuse, match="email, phone or mobile"):
        kt.create(FakeT(partner_email=True), live=True, out=lambda *_: None)


def test_auto_assigning_team_gets_our_own_uid_explicitly():
    t = FakeT(assign="randomly")
    kt.create(t, live=True, out=lambda *_: None)
    assert t.writes[-1][2][0]["user_id"] == 82
    t2 = FakeT()
    kt.create(t2, live=True, out=lambda *_: None)
    assert "user_id" not in t2.writes[-1][2][0]


def test_unknown_own_uid_refuses_on_an_auto_assigning_team():
    t = FakeT(assign="randomly")
    t.uid = 0
    with pytest.raises(kt.Refuse, match="own uid is unknown"):
        kt.create(t, live=True, out=lambda *_: None)
    assert t.writes == []


def test_close_refuses_a_stage_that_has_an_email_template():
    stages = [OPEN, {**SOLVED, "template_id": [5, "Solved email"]},
              {"id": 9, "name": "Live", "template_id": False, "sequence": 4, "is_close": True}]      # 9 auto-closes: never
    t = FakeT(stages=stages)
    with pytest.raises(kt.Refuse, match="no closing stage"):
        kt.close(t, 77, live=True, out=lambda *_: None)
    assert t.writes == []


def test_close_writes_the_stage_only_and_refuses_non_test_tickets():
    t = FakeT()
    kt.close(t, 77, live=True, out=lambda *_: None)
    assert t.writes == [("helpdesk.ticket", "write", [[77], {"stage_id": 3}])]
    t2 = FakeT()
    t2.ticket["name"] = "Initech real ticket"
    with pytest.raises(kt.Refuse, match="not a TEST ticket"):
        kt.close(t2, 77, live=True, out=lambda *_: None)
    assert t2.writes == []


def test_tool_has_no_delete_path():
    src = Path(kt.__file__).read_text(encoding="utf-8").lower()
    assert "unlink" not in src and "delete" not in src.replace("no delete path", "")
    with pytest.raises(guard.WriteRefused):
        guard.check("test_ticket", "helpdesk.ticket", "unlink", [[1]], {})


# ------------------------------------------------------------------------------------------------ template stages

TEMPLATE_STAGES = [{"id": 1, "name": "New", "template_id": [70, "Reception"], "sequence": 1, "fold": False},
                   {"id": 4, "name": "Cancelled", "template_id": [171, "Cancelled"], "sequence": 4, "fold": True},
                   {"id": 9, "name": "Live", "template_id": False, "sequence": 5, "fold": True},
                   {"id": 17, "name": "Other", "template_id": False, "sequence": 6, "fold": True}]


class FoldT(FakeT):
    def execute_kw(self, model, method, args, kwargs):
        if method == "fields_get" and model == "helpdesk.stage":
            return {"fold": {}, "is_close": {}}
        return super().execute_kw(model, method, args, kwargs)


def test_close_on_a_template_stage_needs_the_live_test_contact_and_tracking_off():
    t, lines = FoldT(stages=TEMPLATE_STAGES), []
    kt.close(t, 77, live=True, out=lines.append, close_stage_id=4)       # FakeT asserts the mail-off context
    assert t.writes == [("helpdesk.ticket", "write", [[77], {"stage_id": 4}])]
    assert any("contact verified" in l for l in lines)


@pytest.mark.parametrize("kw", [{"partner_email": True}, {"partner_name": "Real Client"}])
def test_template_stage_refused_when_the_contact_check_fails(kw):
    t = FoldT(stages=TEMPLATE_STAGES, **kw)
    with pytest.raises(kt.Refuse, match="not the TEST contact"):
        kt.close(t, 77, live=True, out=lambda *_: None, close_stage_id=4)
    assert t.writes == []


@pytest.mark.parametrize("sid", [9, 17, 99, 1])
def test_close_stage_must_be_a_folded_team_stage_never_9_or_17(sid):
    t = FoldT(stages=TEMPLATE_STAGES)
    with pytest.raises(kt.Refuse, match="not a folded stage"):
        kt.close(t, 77, live=True, out=lambda *_: None, close_stage_id=sid)
    assert t.writes == []


def test_new_mail_row_prints_a_loud_warning_naming_the_ids():
    t, lines = FoldT(stages=TEMPLATE_STAGES, mail_on_create=1), []
    with pytest.raises(kt.Refuse, match="mail appeared"):
        kt.close(t, 77, live=True, out=lines.append, close_stage_id=4)
    assert any("WARNING" in l and "[101]" in l for l in lines)


def test_create_on_a_template_stage_checks_the_contact_first():
    t, lines = FoldT(stages=TEMPLATE_STAGES), []
    kt.create(t, live=True, out=lines.append)
    assert t.writes[-1][2][0]["stage_id"] == 1 and any("stage 1 has an email template" in l for l in lines)
    with pytest.raises(kt.Refuse, match="not the TEST contact|email, phone or mobile"):
        kt.create(FoldT(stages=TEMPLATE_STAGES, partner_name="x"), live=True, out=lambda *_: None)


def test_M1_skip_mail_table_refuses_any_stage_with_a_template():
    with pytest.raises(kt.Refuse, match="skip-mail-table"):
        kt.create(FoldT(stages=TEMPLATE_STAGES), live=True, out=lambda *_: None, skip_mail_table=True)
    t = FoldT(stages=TEMPLATE_STAGES)
    with pytest.raises(kt.Refuse, match="skip-mail-table"):
        kt.close(t, 77, live=True, out=lambda *_: None, skip_mail_table=True, close_stage_id=4)
    assert t.writes == []
    kt.close(FakeT(), 77, live=True, out=lambda *_: None, skip_mail_table=True)           # silent stages are fine


def test_M2_the_tool_uses_the_same_fold_first_rule_as_the_watcher():
    stages = [{**OPEN, "fold": False}, {**SOLVED, "fold": True},
              {"id": 4, "name": "Cancelled", "template_id": False, "sequence": 4, "fold": False, "is_close": True}]
    with pytest.raises(kt.Refuse, match="not a folded stage"):
        kt.close(FoldT(stages=stages), 77, live=True, out=lambda *_: None, close_stage_id=4)


# ------------------------------------------------------------------------------------------------ the guard allowance

CTX = guard.TEST_CONTEXT


def chk(model, method, args, ctx=None, **kw):
    guard.check("test_ticket", model, method, args, {"context": dict(CTX)} if ctx is None else ctx, **kw)


def create_vals(**over):
    return {"name": "TEST · x", "team_id": 2, "stage_id": 1, "partner_id": 10, **over}


def reader(name="TEST · x", partner=10, team=2, email=False, pname=guard.TEST_PARTNER):
    def fn(model, ids, fields):
        if model == "helpdesk.ticket":
            return [{"name": name, "partner_id": [partner, "p"], "team_id": [team, "t"]}]
        return [{"name": pname, "email": email, "phone": False, "mobile": False}]
    return fn


GOOD = dict(safe_stages=(1,), test_partner_id=10, team_stage_ids=[1, 3, 4], self_uid=82, read_fn=reader())


def test_guard_allows_the_narrow_test_writes_and_only_counts_mail_mail():
    chk("res.partner", "create", [{"name": guard.TEST_PARTNER}])
    chk("helpdesk.ticket", "create", [create_vals()], **GOOD)
    chk("helpdesk.ticket", "write", [[1], {"stage_id": 3}], gated_stage=3, test_partner_id=10, team_stage_ids=[1, 3],
        read_fn=reader())
    guard.check("read", "mail.mail", "search_count", [[]], {})
    with pytest.raises(guard.WriteRefused):
        guard.check("read", "mail.mail", "search_read", [[]], {"fields": ["recipient_ids"]})
    with pytest.raises(guard.WriteRefused):                      # the old purposes did not get wider
        guard.check("helpdesk", "res.partner", "create", [{"name": guard.TEST_PARTNER}], {})


@pytest.mark.parametrize("vals,kw", [
    (create_vals(partner_id=4242), {}),                       # a real partner
    ({k: v for k, v in create_vals().items() if k != "partner_id"}, {}),   # no partner
    (create_vals(partner_id=True), {"test_partner_id": 1}),
    (create_vals(stage_id=9), {"safe_stages": (9,), "team_stage_ids": [1, 9]}),
    (create_vals(stage_id=17), {"safe_stages": (17,), "team_stage_ids": [1, 17]}),
    (create_vals(stage_id=5), {"safe_stages": (5,)}),          # not a stage of the team
    (create_vals(user_id=5), {}), (create_vals(name="Initech"), {}), (create_vals(team_id=3), {}),
    (create_vals(), {"test_partner_id": None})])
def test_H3_guard_refuses_outside_the_create_allowance(vals, kw):
    with pytest.raises(guard.WriteRefused):
        chk("helpdesk.ticket", "create", [vals], **{**GOOD, **kw})


@pytest.mark.parametrize("ctx", [{}, {"context": {}}, {"context": {**CTX, "force_send": True}}])
def test_guard_needs_the_exact_mail_off_context(ctx):
    with pytest.raises(guard.WriteRefused):
        guard.check("test_ticket", "helpdesk.ticket", "create", [create_vals()], ctx, **GOOD)


@pytest.mark.parametrize("kw", [
    {"read_fn": reader(name="Initech real")}, {"read_fn": reader(partner=4242)}, {"read_fn": reader(team=3)},
    {"read_fn": reader(email="a@b.example.org")}, {"read_fn": reader(pname="Real Client")}, {"read_fn": None},
    {"read_fn": reader(), "test_partner_id": None}, {"read_fn": reader(), "gated_stage": 9, "team_stage_ids": [9]},
    {"read_fn": reader(), "team_stage_ids": [1]}])
def test_H3_stage_write_rechecks_the_live_ticket_and_contact_itself(kw):
    base = {"gated_stage": 3, "test_partner_id": 10, "team_stage_ids": [1, 3]}
    with pytest.raises(guard.WriteRefused):
        chk("helpdesk.ticket", "write", [[1], {"stage_id": 9 if kw.get("gated_stage") == 9 else 3}], **{**base, **kw})


def test_guard_allows_user_id_only_when_it_is_the_authenticated_uid():
    chk("helpdesk.ticket", "create", [create_vals(user_id=82)], **GOOD)
    for uid, me in ((83, 82), (82, None), (82, 0), (True, 1), (None, 82)):
        with pytest.raises(guard.WriteRefused):
            chk("helpdesk.ticket", "create", [create_vals(user_id=uid)], **{**GOOD, "self_uid": me})


@pytest.mark.parametrize("rf", [reader(pname="Real Client"), reader(email="a@b.example.org"), None])
def test_L1_create_rereads_the_declared_test_contact_itself(rf):
    with pytest.raises(guard.WriteRefused):
        chk("helpdesk.ticket", "create", [create_vals(partner_id=4242)], **{**GOOD, "test_partner_id": 4242, "read_fn": rf})
