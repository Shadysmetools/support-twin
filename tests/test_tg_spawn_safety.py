"""S1/S2/S3: untrusted ticket text routed through the engine lands as
<untrusted> INBOX DATA - never in a shell command / argv / .bat - and the owner id is per-install, not hard-coded."""
from __future__ import annotations

from pathlib import Path

from twin_agent import session_launch
from twin_agent.config import owner_ids

# A ticket id, a client name and an issue, each carrying shell metacharacters and a newline.
ATTACK_ISSUE = 'receipt" & calc.exe & echo \npwned ; rm -rf / `whoami`'
ATTACK_WHO = 'Mariam" & shutdown /s & "'
ATTACK_TID = "2651; rm -rf /"
_NEEDLES = ('calc.exe', 'rm -rf', 'shutdown', 'whoami', 'pwned', '"', '&', ';', '`', '\n')


def test_untrusted_ticket_text_never_reaches_a_shell_command(tmp_home):
    spawned: list = []
    res = session_launch.launch_ticket_session(
        ATTACK_TID, ATTACK_WHO, ATTACK_ISSUE, specialist="Functional Consultant",
        live_launch=False, spawn=lambda cmd: spawned.append(cmd) or True)
    # observe (default): nothing is spawned; a command is only PREPARED
    assert res.action == "prepared" and res.launched is False and not spawned
    joined = " ".join(res.cmd)
    for needle in _NEEDLES:
        assert needle not in joined, (needle, res.cmd)
    # the tab title comes from the ticket id alone (safe_slug), never the raw text. The default spawn target is
    # "desktop" (cmd.exe /c start <title> ...), so the title is the token right after "start"; a "terminal"
    # target would carry it after "--title". Either way it is a safe_slug, never raw text.
    title = res.cmd[res.cmd.index("--title") + 1] if "--title" in res.cmd \
        else res.cmd[res.cmd.index("start") + 1]
    assert title.startswith("ticket-") and " " not in title
    # the client's name + message live in INBOX.md, wrapped as untrusted DATA (not argv, not executable)
    inbox = Path(res.inbox_path).read_text(encoding="utf-8")
    assert "<untrusted" in inbox and "</untrusted>" in inbox


def test_even_a_live_launch_keeps_user_text_out_of_argv(tmp_home):
    spawned: list = []
    res = session_launch.launch_ticket_session(
        "2651", ATTACK_WHO, ATTACK_ISSUE, specialist="Developer", live_launch=True,
        spawn=lambda cmd: spawned.append(cmd) or True)
    assert res.launched is True and len(spawned) == 1
    joined = " ".join(spawned[0])
    for needle in _NEEDLES:
        assert needle not in joined, (needle, spawned[0])


def test_owner_ids_are_per_install_and_fail_closed():
    assert owner_ids({"people": {"principal_tg_user_id": 777}}) == {777}     # each install = its own owner
    assert owner_ids({"people": {"principal_tg_user_id": 0}}) == set()       # unset -> no owner (fail closed)
    assert owner_ids({}) == set() and owner_ids({"people": {}}) == set()
    assert owner_ids({"people": {"principal_tg_user_id": "x"}}) == set()     # non-numeric -> fail closed
    assert 9000000005 not in owner_ids({"people": {}})                   # no hard-coded fallback


def test_demo_bot_is_removed():
    """The unsafe demo bot (DM->Claude with no guards, guardless autopilot, 'never say you are an AI') is gone.
    Client-facing work goes only through the real gated pipeline (listener -> router -> gated Sender)."""
    bot = Path(__file__).resolve().parents[1] / "telegram" / "tg_principal_bot.py"
    assert not bot.exists(), "the unsafe demo bot must stay removed (issue #43)"
