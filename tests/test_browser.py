"""BUILD 3: the Digital Twin browser (CDP attach helper, login broker, read-only work-site history, launcher)."""
from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from twin_agent import creds
from twin_agent.browser import broker, history
from twin_agent.browser.attach import CDP, CDPRefused, check_method, check_url

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "digital-twin-browser.ps1"
PS = shutil.which("powershell") or shutil.which("pwsh")


class FakeChrome:
    def __init__(self):
        self.tabs, self.calls, self.n = {"other": {"id": "other", "url": "https://mail.example.test"}}, [], 0

    def __call__(self, req, timeout):
        path = req.full_url.split("9334", 1)[1]
        self.calls.append((req.get_method(), path))
        if path.startswith("/json/new"):
            self.n += 1
            tid = f"t{self.n}"
            self.tabs[tid] = {"id": tid, "url": path.split("?", 1)[1]}
            body = self.tabs[tid]
        elif path.startswith("/json/close/"):
            self.tabs.pop(path.rsplit("/", 1)[1], None)
            body = "Target is closing"
        elif path == "/json/list":
            body = list(self.tabs.values())
        else:
            body = {"Browser": "Chrome/140"}
        return io.BytesIO((body if isinstance(body, str) else json.dumps(body)).encode())


def test_cdp_is_loopback_only_and_opens_and_closes_only_its_own_tabs():
    with pytest.raises(CDPRefused):
        CDP(9334, host="192.168.1.5")
    chrome = FakeChrome()
    cdp = CDP(9334, opener=chrome)
    assert cdp.endpoint() == "http://127.0.0.1:9334" and cdp.version()["Browser"]
    with cdp.tab("https://sys.example.test/odoo/helpdesk") as t:
        assert t["id"] in chrome.tabs and [x["id"] for x in cdp.own_tabs()] == [t["id"]]
    assert t["id"] not in chrome.tabs and "other" in chrome.tabs
    with pytest.raises(CDPRefused):
        cdp.close_tab("other")                                   # never someone else's tab
    assert ("GET", "/json/close/other") not in chrome.calls


@pytest.mark.parametrize("m", ["Network.getAllCookies", "Network.getCookies", "Storage.getCookies",
                               "Target.attachToTarget", "Browser.close", "Storage.getUsage"])
def test_cookie_and_target_methods_are_denied(m):
    with pytest.raises(CDPRefused):
        check_method(m)


def test_allowed_methods_and_urls():
    check_method("Page.navigate")
    check_method("Runtime.evaluate")
    assert check_url("https://sys.example.test/x")
    for bad in ("javascript:alert(1)", "file:///C:/TwinAgent/vault", "http://evil.example.test/"):
        with pytest.raises(CDPRefused):
            check_url(bad)


class FakePage:
    def __init__(self, url):
        self.url, self.filled, self.clicked = url, {}, []

    def fill(self, sel, value):
        self.filled[sel] = value

    def click(self, sel):
        self.clicked.append(sel)


SITES = {"browser": {"bitwarden": False, "sites": {"sys": {
    "url_prefix": "https://sys.example.test/web/login", "user_selector": "#login", "pass_selector": "#password",
    "submit_selector": "button[type=submit]", "bitwarden_item": "item-1"}}}}


def test_broker_fills_from_the_vault_and_returns_no_value(tmp_home, fake_keyring):
    creds.set_secret("site-logins", json.dumps({"sys": {"username": "principal@example.org",
                                                        "password": "Sample-Pass-123"}}), backend=fake_keyring)
    page = FakePage("https://sys.example.test/web/login?redirect=/odoo")
    res = broker.fill_login(page, "sys", SITES, backend=fake_keyring)
    assert res == "filled" and page.filled == {"#login": "principal@example.org", "#password": "Sample-Pass-123"}
    assert "Sample-Pass" not in res and page.clicked == ["button[type=submit]"]
    look_alike = FakePage("https://sys.example.test.evil.test/web/login")
    assert broker.fill_login(look_alike, "sys", SITES, backend=fake_keyring).startswith("refused")
    assert look_alike.filled == {}
    assert broker.fill_login(FakePage("http://sys.example.test/web/login"), "sys", SITES,
                             backend=fake_keyring).startswith("refused")      # never over plain http
    assert broker.fill_login(page, "github", SITES, backend=fake_keyring).startswith("no login config")
    audit = (tmp_home / "data" / "logs" / "vault-audit.jsonl")
    assert not audit.exists() or "Sample-Pass" not in audit.read_text(encoding="utf-8")


def test_broker_bitwarden_path(tmp_home, fake_keyring):
    creds.set_secret("bitwarden-session", "bw-session-sample", backend=fake_keyring)
    seen = {}

    def bw(cmd, env):
        seen["cmd"], seen["has_session"] = cmd, env.get("BW_SESSION") == "bw-session-sample"
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"login": {"username": "u", "password": "p-sample"}}), "")
    cfg = json.loads(json.dumps(SITES))
    cfg["browser"]["bitwarden"] = True
    page = FakePage("https://sys.example.test/web/login")
    assert broker.fill_login(page, "sys", cfg, backend=fake_keyring, bw_run=bw) == "filled"
    assert seen == {"cmd": ["bw", "get", "item", "item-1"], "has_session": True} and page.filled["#password"] == "p-sample"


def _history(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                      "CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);")
    for i, (url, title, ts) in enumerate(rows, 1):
        con.execute("INSERT INTO urls VALUES (?,?,?)", (i, url, title))
        con.execute("INSERT INTO visits VALUES (?,?,?)", (i, i, history.to_chrome(ts)))
    con.commit()
    con.close()


def test_history_is_copied_read_only_and_filtered_to_work_domains(tmp_home, cfg):
    t = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)
    src = tmp_home / "browser" / "DigitalTwin" / "Default" / "History"
    _history(src, [("https://helpdesk.example.com/odoo/helpdesk/2/2636?access_token=abc123&id=2636", "Ticket 2636", t),
                   ("https://www.youtube.com/watch?v=x", "Personal", t + timedelta(minutes=1)),
                   ("https://github.com/org/repo/pull/12", "PR", t + timedelta(minutes=2)),
                   ("https://helpdesk.example.com/web#id=2650&model=helpdesk.ticket&menu_id=9", "x", t + timedelta(days=2))])
    before = src.read_bytes()
    assert history.visits(cfg, t - timedelta(hours=1), t + timedelta(hours=1)) == []   # gate off (no D4)
    cfg["consent"]["d4_watcher_ref"] = "D4-test"
    cfg["browser"]["history"]["enabled"] = True
    vs = history.visits(cfg, t - timedelta(hours=1), t + timedelta(hours=1))
    assert [(v["domain"], v["path"]) for v in vs] == [("helpdesk.example.com", "/odoo/helpdesk/2/2636?id=2636"),
                                                      ("github.com", "/org/repo/pull/12")]
    assert all("access_token" not in v["path"] and v["ts"].tzinfo for v in vs)
    assert src.read_bytes() == before and not list(src.parent.glob("History-journal"))
    later = history.visits(cfg, t + timedelta(days=1), t + timedelta(days=3))
    assert later[0]["path"] == "/web#id=2650&model=helpdesk.ticket"
    from twin_agent.timesheets.activity import history_acts
    assert history_acts(vs)[0].refs[0] == "U:2636"


def test_launcher_is_ascii_and_refuses_the_normal_profile():
    text = SCRIPT.read_text(encoding="ascii")
    assert "--remote-debugging-address=127.0.0.1" in text and "9334" in text
    assert "cookies" in text.lower() and "export" in text.lower()


@pytest.mark.skipif(PS is None, reason="no PowerShell")
def test_launcher_parses_and_dry_runs(tmp_path):
    cmd = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
           f"'{SCRIPT}', [ref]$null, [ref]$e); $e.Count")
    out = subprocess.run([PS, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "0", out.stdout + out.stderr
    out = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-DryRun",
                          "-AgentHome", str(tmp_path), "-Port", "9"], capture_output=True, text=True, timeout=60)
    assert "REFUSED: bad port" in out.stdout
    out = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-DryRun",
                          "-AgentHome", str(tmp_path), "-Port", "9399"], capture_output=True, text=True, timeout=60)
    assert "DRY RUN" in out.stdout and "--remote-debugging-port=9399" in out.stdout
    assert not (tmp_path / "browser").exists()
