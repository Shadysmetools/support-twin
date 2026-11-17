"""Guard purpose ``client_contact`` (intake v2): ONE res.partner create / write with name, phone, company, tag link."""
from __future__ import annotations

import pytest

from twin_agent.helpdesk import guard
from twin_agent.hub.odoo_gateway import GuardedOdoo
from twin_agent.testing.fake_odoo import FakeOdoo, _seed

CTX = dict(guard.TEST_CONTEXT)


def chk(method, args, kwargs=None, model="res.partner"):
    guard.check("client_contact", model, method, args, {"context": CTX} if kwargs is None else kwargs)


def test_allowed_create_and_write():
    chk("create", [{"name": "Nina Leo", "phone": "+201000000001"}])
    chk("create", [{"name": "Nina Leo", "mobile": "201000000001", "parent_id": 7, "category_id": [(4, 3)]}])
    chk("write", [[5], {"parent_id": 7}])
    chk("write", [[5], {"category_id": [(4, 3)]}])


@pytest.mark.parametrize("method,args", [
    ("create", [{"name": "A B", "phone": "+201000000001", "email": "a@b.example.org"}]),               # never an email
    ("create", [{"name": "A B", "phone": "+201000000001", "user_ids": [(4, 1)]}]),            # never a user
    ("create", [{"name": "A B", "phone": "+201000000001", "is_company": True}]),              # not on the allowlist
    ("create", [{"name": "A B", "phone": "+201000000001", "category_id": [(0, 0, {"name": "x"})]}]),
    ("create", [{"name": "A B", "phone": "+201000000001", "category_id": [(6, 0, [1])]}]),
    ("create", [{"name": "A B", "phone": "+201000000001", "parent_id": "7"}]),
    ("create", [{"name": "A B", "phone": "+201000000001", "parent_id": 0}]),
    ("create", [{"name": "A B"}]),                                                           # no phone
    ("create", [{"name": "A B", "phone": "call me"}]),
    ("create", [{"name": "x" * 81, "phone": "+201000000001"}]),
    ("create", [{"name": "", "phone": "+201000000001"}]),
    ("create", [{"name": "Acme Test Client – Riley", "phone": "+201000000001"}]),         # a test name is never a real contact
    ("create", [{"name": "A‮B", "phone": "+201000000001"}]),                            # a bidi control character
    ("create", [{"name": "A<b>", "phone": "+201000000001"}]),
    ("create", [[{"name": "A", "phone": "+201000000001"}, {"name": "B", "phone": "+201000000002"}]]),
    ("write", [[5], {"name": "Renamed"}]),                                                   # a write never renames
    ("write", [[5], {"phone": "+201000000009"}]),
    ("write", [[5], {"email": "a@b.example.org"}]),
    ("write", [[5, 6], {"parent_id": 7}]),                                                   # one record only
    ("write", [[5], {}]),
    ("unlink", [[5]]),
])
def test_refused(method, args):
    with pytest.raises(guard.WriteRefused):
        chk(method, args)


def test_refused_context_and_model():
    for kw in ({}, {"context": {"tracking_disable": True}}, {"context": {**CTX, "force_send": True}},
               {"context": CTX, "x": 1}):
        with pytest.raises(guard.WriteRefused):
            chk("create", [{"name": "A B", "phone": "+201000000001"}], kw)
    with pytest.raises(guard.WriteRefused):
        chk("create", [{"name": "A B"}], model="helpdesk.ticket")
    with pytest.raises(guard.WriteRefused):
        guard.check("client_contact", "res.users", "create", [{"name": "A"}], {"context": CTX})


def gw(on=True):
    t = FakeOdoo(data=_seed())
    t.data["res.partner"][5001] = {"id": 5001, "name": "Nina", "is_company": False, "parent_id": False,
                                   "phone": "+201000000001", "user_ids": False}
    t.data["res.partner"][5003] = {"id": 5003, "name": "Has Company", "is_company": False, "parent_id": [3001, "Co"]}
    t.data["res.partner"][5004] = {"id": 5004, "name": "Acme Test Client – Riley", "is_company": False,
                                   "parent_id": False}
    return GuardedOdoo(t, writes_on=on), t


def test_gateway_refuses_to_create_when_any_partner_has_the_phone():
    """The hub only creates when partner_for found no client: a partner with this number (any format, a company too) means
    a human must decide. Never a reuse, never a duplicate."""
    g, t = gw()
    for phone in ("+201000000001", "+20 100 000 0001"):
        with pytest.raises(guard.WriteRefused):
            g.create_client_contact("Nina Again", phone)
    t.data["res.partner"][5009] = {"id": 5009, "name": "A Company", "is_company": True, "mobile": "+201000000055"}
    with pytest.raises(guard.WriteRefused):
        g.create_client_contact("Newcomer", "+201000000055")
    assert not t.writes
    new = g.create_client_contact("Newcomer", "+201000000077", 3001)
    assert t.writes[-1]["vals"] == {"name": "Newcomer", "phone": "+201000000077", "parent_id": 3001} and new


def test_gateway_link_only_a_person_without_a_company():
    g, t = gw()
    g.link_client_contact(5001, 3001)
    assert t.writes[-1]["vals"] == {"parent_id": 3001}
    for pid in (5003, 5004, 3001):                          # has a company / a test client / a company record
        with pytest.raises(guard.WriteRefused):
            g.link_client_contact(pid, 3002)


def test_gateway_writes_off_refuses():
    g, t = gw(on=False)
    with pytest.raises(guard.WriteRefused):
        g.create_client_contact("Newcomer", "+201000000077")
    assert not t.writes
