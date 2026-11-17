"""Hard Flow-safety ban (Odoo safety review 2026-11-04): the browser and the proof-capture path may never
reach the Globex Odoo tenant, because production AND every Globex dev build (odoo.sh / stagelatest) share the
LIVE Flow warehouse + Shopify keys — a single click there can push a real fulfilment / order. The ban is
hard-coded and NOT clearable by config."""
from __future__ import annotations

import pytest

from twin_agent.browser import broker
from twin_agent.browser.attach import CDPRefused, check_url, is_flow_tenant, is_odoo_host
from twin_agent.proof import pipeline


def test_browser_allows_safe_non_odoo_hosts():
    assert check_url("https://sandbox.example.test/x")        # a genuinely unrelated host
    assert check_url("https://accounts.google.com/signin")   # a non-Odoo work-site login page
    assert check_url("about:blank")


@pytest.mark.parametrize("banned", [
    "https://globex.odoo.com/web",                                   # production, Flow ON
    "https://globex-stagelatest-10000001.dev.odoo.com/odoo",         # stagelatest, shares Flow
    "https://globex-eg.odoo.sh/web",                                 # an Globex odoo.sh dev build — also Flow-risky
])
def test_browser_hard_bans_every_globex_odoo_host(banned):
    with pytest.raises(CDPRefused):
        check_url(banned)


def test_is_flow_tenant():
    assert is_flow_tenant("globex.odoo.com")
    assert is_flow_tenant("globex-stagelatest-10000001.dev.odoo.com")
    assert not is_flow_tenant("helpdesk.example.com")
    assert not is_flow_tenant(None)


def test_proof_capture_flow_blocked_by_default():
    # even run_mode=live gets the flow blocker when no explicit non-Globex sandbox is set
    assert "Globex Flow" in (pipeline._flow_blocked({"run_mode": "live"}) or "")


def test_proof_capture_flow_not_clearable_by_config():
    cfg = {"run_mode": "live", "proof": {"capture_base": "https://globex.odoo.com", "cleared": ["flow", "qa_user"]}}
    assert "FLOW SAFETY" in (pipeline._flow_blocked(cfg) or "")


def test_proof_capture_flow_cleared_only_by_real_sandbox():
    assert pipeline._flow_blocked({"proof": {"capture_base": "https://sandbox.example.test"}}) is None


# ---------------------------------------------------------------- S7: Odoo is API-only, never a browser

@pytest.mark.parametrize("odoo", [
    "https://helpdesk.example.com/web",                                   # Acme production Odoo
    "https://HELPDESK.EXAMPLE.COM/web",                                   # case-insensitive
    "https://helpdesk.example.com./web",                                  # trailing dot
    "https://user@helpdesk.example.com/web",                              # userinfo in the URL
    "https://globex.odoo.com/web",                                   # Odoo Online
    "https://sandbox.example.odoo.com/web",                          # any *.odoo.com
    "https://globex-stagelatest-10000001.dev.odoo.com/odoo",         # Odoo dev build
])
def test_browser_refuses_every_odoo_host(odoo):
    with pytest.raises(CDPRefused):
        check_url(odoo)


@pytest.mark.parametrize("spoof", [
    "https://helpdesk.example.com\\@sandbox.example.org/",     # backslash: Chrome goes to helpdesk.example.com
    "https://sandbox.example.test\\@helpdesk.example.com/",     # the other way round
    "https://user@sandbox.example.org/",                  # userinfo '@' (urlsplit vs browser can disagree)
])
def test_browser_refuses_host_spoofing_urls(spoof):
    with pytest.raises(CDPRefused):
        check_url(spoof)


def test_is_odoo_host():
    for h in ("helpdesk.example.com", "HELPDESK.EXAMPLE.COM", "helpdesk.example.com.", "globex.odoo.com", "sub.odoo.com",
              "globex-stagelatest-10000001.dev.odoo.com", "odoo.com"):
        assert is_odoo_host(h), h
    # NOT banned here: the generic Odoo.sh devops console (Globex's own odoo.sh is handled by is_flow_tenant),
    # plain example.com, and look-alikes.
    for h in ("sandbox.example.test", "accounts.google.com", "example.com", "www.odoo.sh", "x.odoo.sh",
              "notodoo.com.evil.test", "evil-odoo.com", "", None):
        assert not is_odoo_host(h), h


class _FakePage:
    """A page whose fill/click must never be reached when it is on an Odoo host."""
    def __init__(self, url):
        self.url = url

    def fill(self, *a):            # pragma: no cover - must not run
        raise AssertionError("broker must not fill a login on an Odoo host")

    def click(self, *a):          # pragma: no cover - must not run
        raise AssertionError("broker must not click on an Odoo host")


def test_broker_refuses_odoo_login_page():
    sites = {"sys": {"url_prefix": "https://helpdesk.example.com/web/login", "user_selector": "#u",
                     "pass_selector": "#p", "submit_selector": "#s"}}
    res = broker.fill_login(_FakePage("https://helpdesk.example.com/web/login"), "sys", {"browser": {"sites": sites}})
    assert res.startswith("refused") and "Odoo" in res            # never reaches the vault or page.fill


def test_proof_capture_refuses_non_globex_odoo_sandbox():
    # an Odoo sandbox (odoo.com / odoo.sh) is NOT a valid capture target: Odoo is API-only
    assert "ODOO API-ONLY" in (pipeline._flow_blocked({"proof": {"capture_base": "https://sandbox.example.odoo.com"}}) or "")
    assert "ODOO API-ONLY" in (pipeline._flow_blocked({"proof": {"capture_base": "https://helpdesk.example.com"}}) or "")
