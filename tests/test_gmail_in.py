"""gmail_in + gmail_common: parsing, allow-list (default deny), history paging, expired history, poller."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest
import yaml

from twin_agent.channels import gmail_common as gc
from twin_agent.channels import gmail_in
from twin_agent.channels.gmail_in import GmailPoller, RealGmailIn, admit, normalize_gmail
from twin_agent.models import Channel
from twin_agent.testing.fakes import FakeGmail
from gmail_fakes import FakeService, raw_msg

REPO = Path(__file__).resolve().parents[1]
SME = '"Staff Name | Globex" <info@example.com>'


@pytest.fixture
def gcfg() -> dict:
    c = yaml.safe_load((REPO / "config.example.yaml").read_text(encoding="utf-8"))
    c["mailboxes"]["ticketmail"]["address"] = "principal@example.org"
    return c


@pytest.fixture
def live(gcfg, monkeypatch):
    from twin_agent import creds
    monkeypatch.setattr(creds, "has_secret", lambda name, **k: True)
    c = copy.deepcopy(gcfg)
    c["run_mode"] = "live"
    return c


def test_parse_plain_html_attachments_and_quotes(gcfg):
    raw = raw_msg("m1", frm=SME, subject="Re: Fix X (#9001)",
                  body="New line here\n\nOn Sun, Oct 25, 2026 Someone <a@b.example.org>\nwrote:" "\n> old text",
                  attachments=("shot 1.png",))
    ev = gc.to_event(raw, "ticketmail", own_addresses=["principal@example.org"])
    assert ev.event_id == "gm:ticketmail:m1"
    assert ev.text == "New line here"
    assert ev.sender.email == "info@example.com" and not ev.sender.is_principal
    assert ev.media == ["gmail-att:m1:att0:shot 1.png"]
    assert ev.ts.tzinfo is not None and str(ev.ts.tzinfo) == "Africa/Cairo"
    assert ev.headers["message-id"] == "<m1@mail.example.org>"
    html = raw_msg("m2", frm=SME, body="<p>Hello&nbsp;there​</p><div>Line͏ 2</div>", html=True)
    assert gc.to_event(html, "x", own_addresses=[]).text.split("\n") == ["Hello\xa0there", "Line 2"]


def test_display_name_never_trusted():
    assert gc.address('"help@example.com" <evil@example.org>') == "evil@example.org"
    assert gc.domain_match("x@helpdesk.example.com", ["example.com"])
    assert not gc.domain_match("x@example.com.evil.example.net", ["example.com"])


def test_default_deny_and_ticket_channel(gcfg):
    stranger = normalize_gmail(raw_msg("a", frm="Friend <friend@example.org>"), gcfg, "ticketmail")
    assert stranger is None                                    # personal mail dropped in memory
    sme = normalize_gmail(raw_msg("b", frm=SME, body="x\n> The reference of your ticket is 9001"), gcfg,
                          "ticketmail")
    assert sme.channel == Channel.ticket_mail and "9001" in sme.text   # quotes kept for ticket mail
    spam_label = normalize_gmail(raw_msg("c", frm=SME, labels=("SPAM",)), gcfg, "ticketmail")
    assert spam_label is None


def test_own_mail_only_to_allowed_party(gcfg):
    me = "Principal <principal@example.org>"
    to_help = raw_msg("a", frm=me, to="help@example.com", labels=("SENT",))
    to_friend = raw_msg("b", frm=me, to="friend@example.org", labels=("SENT",))
    assert normalize_gmail(to_help, gcfg, "ticketmail").sender.is_principal
    assert normalize_gmail(to_friend, gcfg, "ticketmail") is None


def test_globex_mailbox_empty_allow_list_denies(gcfg):
    ev = gc.to_event(raw_msg("a", frm="x@globex.example.net"), "globex", own_addresses=[])
    assert not admit(ev, gcfg, "globex", ["INBOX"])


def test_real_adapter_refuses_in_dry_run(gcfg):
    adapter = RealGmailIn(gcfg, service_factory=lambda c, m: FakeService())
    with pytest.raises(gc.AdapterRefused):
        adapter.list_new("ticketmail", None)


def test_real_adapter_refuses_without_vault(gcfg, monkeypatch):
    from twin_agent import creds
    monkeypatch.setattr(creds, "has_secret", lambda name, **k: name != "gmail-ticketmail-refresh")
    gcfg["run_mode"] = "live"
    with pytest.raises(gc.AdapterRefused):
        RealGmailIn(gcfg, service_factory=lambda c, m: FakeService()).list_new("ticketmail", "1")


def test_history_baseline_paging_and_filter(live):
    svc = FakeService()
    adapter = RealGmailIn(live, service_factory=lambda c, m: svc)
    events, cursor = adapter.list_new("ticketmail", None)
    assert events == [] and cursor == "100"                    # baseline only, no backlog flood
    for i in range(5):
        svc.add(raw_msg(f"m{i}", frm=SME if i != 2 else "x@example.org", ts_ms=1793536800000 + i))
    events, cursor = adapter.list_new("ticketmail", "100")
    assert [e.msg_id for e in events] == ["m0", "m1", "m3", "m4"]
    assert cursor == "105"
    assert sum(1 for c, _ in svc.calls if c == "history.list") == 3   # page size 2 -> 3 pages


def test_expired_history_falls_back(live):
    svc = FakeService()
    svc.add(raw_msg("m1", frm=SME))
    svc.history_expired = True
    events, cursor = RealGmailIn(live, service_factory=lambda c, m: svc).list_new("ticketmail", "5")
    assert [e.msg_id for e in events] == ["m1"] and cursor == "101"
    assert any(c == "messages.list" and kw["q"] == gmail_in.FALLBACK_QUERY for c, kw in svc.calls)


def test_deleted_message_skipped_and_attachment_images_only(live, tmp_path):
    svc = FakeService()
    svc.hist.append((101, "gone"))
    svc.history_id = 101
    adapter = RealGmailIn(live, service_factory=lambda c, m: svc)
    assert adapter.list_new("ticketmail", "100")[0] == []
    assert adapter.download_attachment("ticketmail", "gmail-att:m1:att0:clip.mp4", tmp_path) is None
    p = adapter.download_attachment("ticketmail", "gmail-att:m1:att0:shot.png", tmp_path)
    assert p.read_bytes() == b"PNGDATA"


class Cursors:
    def __init__(self):
        self.d: dict[str, str] = {}

    def get_cursor(self, s):
        return self.d.get(s)

    def set_cursor(self, s, v):
        self.d[s] = v


def test_poller_moves_cursor_after_handover(gcfg, make_event):
    fake = FakeGmail()
    fake.add(make_event(text="one"))
    fake.add(make_event(text="two", msg_id=2))
    seen = []

    async def on_event(ev):
        seen.append(ev.text)

    cur = Cursors()
    poller = GmailPoller(fake, cur, gcfg, on_event)
    assert poller.mailboxes() == ["ticketmail"]               # globex is disabled
    assert asyncio.run(poller.poll_once("ticketmail")) == 2
    assert seen == ["one", "two"] and cur.d["gmail:ticketmail"] == "2"
    assert asyncio.run(poller.poll_once("ticketmail")) == 0


def test_poller_error_reports_class_only(gcfg):
    class Boom:
        def list_new(self, m, h):
            raise RuntimeError("secret-ish text")

    errors = []
    poller = GmailPoller(Boom(), Cursors(), gcfg, lambda ev: None, on_error=lambda m, k: errors.append((m, k)))
    poller.REPORT_AFTER = 1

    async def go():
        task = asyncio.create_task(poller.run())
        await asyncio.sleep(0.01)
        poller.stop()
        await task

    asyncio.run(go())
    assert errors == [("ticketmail", "RuntimeError")]


def test_ticket_mail_tokens_scrubbed_at_intake(gcfg):
    from twin_agent.channels.tickets import parse_ticket_mail
    body = ("Your request X has been received. The reference of your ticket is 9001.\n"
            "> https://helpdesk.example.com/my/ticket/9001/00000000-aaaa-bbbb-cccc-000000000000\n"
            "> https://helpdesk.example.com/my/ticket/9001?access_token=abcdef0123456789")
    ev = normalize_gmail(raw_msg("t", frm=SME, subject="X (#9001)", body=body), gcfg, "ticketmail")
    assert "00000000-aaaa" not in ev.text and "abcdef0123456789" not in ev.text
    upd = parse_ticket_mail(ev)
    assert upd.ticket_id == "9001" and upd.ticket_url == "https://helpdesk.example.com/my/ticket/9001"


def test_poller_resets_the_client_after_an_error_and_reports_back_to_normal(gcfg):
    """2026-11-10: after SSLEOFError the cached Google client kept failing. A failed poll drops it; the next clean
    poll reports ok so a later outage gets its own card. The cursor never moves on a failed poll (no mail lost)."""
    class Flaky:
        def __init__(self):
            self.calls, self.resets = 0, []

        def list_new(self, m, h):
            self.calls += 1
            if self.calls == 1:
                raise OSError("SSLEOFError")
            return [], "77"

        def reset(self, m):
            self.resets.append(m)

    g, cur, errors, oks = Flaky(), Cursors(), [], []
    poller = GmailPoller(g, cur, gcfg, lambda ev: None, on_error=lambda m, k: errors.append(k),
                         on_ok=lambda m: oks.append(m))
    gcfg["mailboxes"]["ticketmail"]["poll_seconds"] = 0

    async def go():
        task = asyncio.create_task(poller.run())
        await asyncio.sleep(0.05)
        poller.stop()
        await task

    poller.REPORT_AFTER = 1
    asyncio.run(go())
    assert errors[:1] == ["OSError"] and g.resets == ["ticketmail"] and oks and cur.d["gmail:ticketmail"] == "77"


def test_poller_times_out_a_hung_poll(gcfg, monkeypatch):
    import time as _t

    class Hang:
        def __init__(self):
            self.resets = 0

        def list_new(self, m, h):
            _t.sleep(0.5)
            return [], "1"

        def reset(self, m):
            self.resets += 1

    g = Hang()
    poller = GmailPoller(g, Cursors(), gcfg, lambda ev: None)
    monkeypatch.setattr(GmailPoller, "POLL_TIMEOUT_S", 0.05)
    try:
        asyncio.run(poller.poll_once("ticketmail"))
        raise AssertionError("should time out")
    except asyncio.TimeoutError:
        pass
    assert g.resets == 1


def test_poller_reports_only_after_three_failures_in_a_row(gcfg):
    class Fails:
        def __init__(self, n):
            self.n = n

        def list_new(self, m, h):
            if self.n > 0:
                self.n -= 1
                raise TimeoutError()
            return [], "5"

    async def run_for(poller):
        task = asyncio.create_task(poller.run())
        await asyncio.sleep(0.05)
        poller.stop()
        await task

    gcfg["mailboxes"]["ticketmail"]["poll_seconds"] = 0
    errors = []
    asyncio.run(run_for(GmailPoller(Fails(2), Cursors(), gcfg, lambda ev: None, on_error=lambda m, k: errors.append(k))))
    assert errors == []                                   # two blips then fine: no card
    asyncio.run(run_for(GmailPoller(Fails(4), Cursors(), gcfg, lambda ev: None, on_error=lambda m, k: errors.append(k))))
    assert errors and set(errors) == {"TimeoutError"}


def test_today_catch_up_hands_over_all_of_todays_mail_once_per_day(gcfg):
    """2026-11-10: when the listener starts and when the day changes, all of today's admitted mail is handed
    over (the router skips what it already has); not again the same day."""
    class G:
        def __init__(self):
            self.days = []

        def list_today(self, m, day):
            self.days.append(day)
            return ["mail-1", "mail-2"]

        def list_new(self, m, h):
            return [], "5"

    seen, g = [], G()

    async def on_event(ev):
        seen.append(ev)

    poller = GmailPoller(g, Cursors(), gcfg, on_event)
    poller.today = lambda: "2026-11-10"
    assert asyncio.run(poller.catch_up_today("ticketmail")) == 2
    assert asyncio.run(poller.catch_up_today("ticketmail")) == 0            # same day: not again
    poller.today = lambda: "2026-11-11"
    assert asyncio.run(poller.catch_up_today("ticketmail")) == 2            # new day: again
    assert g.days == ["2026-11-10", "2026-11-11"] and seen == ["mail-1", "mail-2"] * 2
    gcfg["mailboxes"]["ticketmail"]["today_catchup"] = False
    poller.today = lambda: "2026-11-12"
    assert asyncio.run(poller.catch_up_today("ticketmail")) == 0


class _ProbeService:
    """Fake Gmail service whose getProfile().execute() records how many threads are inside it at once."""

    def __init__(self, hold=None, delay=0.2):
        import threading
        self.active = 0
        self.peak = 0
        self.calls = 0
        self.hold = hold
        self.delay = delay
        self._mx = threading.Lock()

    def users(self):
        return self

    def getProfile(self, userId):
        return self

    def execute(self):
        import time
        with self._mx:
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.hold is not None:
                self.hold.wait(5)
            else:
                time.sleep(self.delay)
            return {"historyId": "42"}
        finally:
            with self._mx:
                self.active -= 1


def test_one_gmail_client_is_never_used_by_two_threads_at_once(live):
    """11 Nov 2026: the startup sweep and the 2-minute poller read Gmail at the same moment through one shared
    httplib2 connection. pythonw died in libssl-3.dll / libcrypto-3.dll (0xC0000005) at 15:56 and 23:04, each a
    few seconds after a start, next to a sweep_gmail_failed SSLError. Calls on one client must take turns."""
    import threading
    svc = _ProbeService()
    adapter = RealGmailIn(live, service_factory=lambda c, m: svc)
    threads = [threading.Thread(target=adapter.list_new, args=("ticketmail", None)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert svc.calls == 3
    assert svc.peak == 1


def test_after_reset_a_new_call_does_not_wait_for_a_hung_old_one(live):
    """A poll that timed out keeps its thread; reset() must give the next poll its own fresh client and lock."""
    import threading
    release = threading.Event()
    old, new = _ProbeService(hold=release), _ProbeService(delay=0)
    made = iter([old, new])
    adapter = RealGmailIn(live, service_factory=lambda c, m: next(made))
    stuck = threading.Thread(target=adapter.list_new, args=("ticketmail", None))
    stuck.start()
    for _ in range(100):
        if old.active:
            break
        __import__("time").sleep(0.01)
    adapter.reset("ticketmail")
    done = threading.Event()
    threading.Thread(target=lambda: (adapter.list_new("ticketmail", None), done.set())).start()
    assert done.wait(2), "the fresh client waited on the old hung call"
    release.set()
    stuck.join(5)
