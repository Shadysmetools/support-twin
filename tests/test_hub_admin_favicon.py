"""Chrome asks for /favicon.ico while the login form is open. That request must not touch the pre-auth CSRF cookie."""
from __future__ import annotations

import re

from admin_helpers import PW, Client, make_app


def _csrf(body: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', body).group(1)


def test_favicon_between_form_and_post_does_not_break_login():
    app, _hub, _info = make_app()
    c = Client(app)
    form = c.get("/login")
    fav = c.get("/favicon.ico")
    assert fav.status == 404 and fav.set_cookies == [] and "Location" not in fav.h
    r = c.post("/login", {"csrf": _csrf(form.body), "username": "demo-approver", "password": PW})
    assert r.status == 303 and r.h["Location"] == "/tickets"


def test_favicon_sets_no_cookie_even_with_a_session():
    c = Client(make_app()[0])
    c.login()
    r = c.get("/favicon.ico")
    assert r.status == 404 and r.set_cookies == []


def test_login_form_reuses_a_valid_pre_cookie():
    c = Client(make_app()[0])
    first = c.get("/login")
    second = c.get("/login")
    assert _csrf(first.body) == _csrf(second.body)
    assert c.cookies["__Host-aa_pre"] == _csrf(first.body)


def test_a_junk_pre_cookie_is_replaced_not_reused():
    c = Client(make_app()[0])
    r = c.request("GET", "/login", cookies={"__Host-aa_pre": "x"})
    assert _csrf(r.body) != "x" and len(_csrf(r.body)) >= 24


def test_other_unauthenticated_gets_do_not_set_the_pre_cookie():
    c = Client(make_app()[0])
    for p in ("/", "/tickets", "/numbers", "/connect", "/nope", "/static/admin.css"):
        assert c.get(p).set_cookies == [], p
