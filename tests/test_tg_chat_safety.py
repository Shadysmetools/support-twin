"""S-web: the local web chat server (telegram/chat_server.py) must never pass a web message through a shell or
into argv. A crafted message is routed through the SAME safe engine path the demo bot uses (session_launch): it
lands in a lane INBOX.md as REDACTED <untrusted> DATA, and reaches the twin brain ONLY on stdin - never a shell,
never a .bat, never argv. The brain itself runs with tools denied and NOT in bypass mode."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_CHAT_SERVER = _REPO / "telegram" / "chat_server.py"


def _load_chat_server():
    spec = importlib.util.spec_from_file_location("telegram_chat_server", _CHAT_SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# A web message carrying shell metacharacters and a newline - every classic RCE breakout char.
ATTACK = 'hi" & calc.exe | whoami ; shutdown /s & `id`\npwned'
_NEEDLES = ("calc.exe", "whoami", "shutdown", "pwned", '"', "&", "|", ";", "`", "\n")


def test_web_message_cannot_reach_a_shell_or_argv(tmp_home, monkeypatch):
    """The core proof: the attack message never reaches a shell/argv; it only lands as <untrusted> INBOX data
    and is fed to the brain on stdin."""
    cs = _load_chat_server()

    # If anything tries to spawn a shell tab, fail loudly - the fixed server must not Popen at all.
    monkeypatch.setattr(cs.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Popen must not be called")))

    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = list(argv)
        captured["kw"] = kw

        class R:
            stdout = "ok"
            stderr = ""
        return R()

    body = json.dumps({"message": ATTACK}).encode()
    out = cs.handle_ask(body, run=fake_run)

    # a shell is NEVER used
    assert captured["kw"].get("shell") in (None, False)
    # no breakout char ever appears in any argv element
    for element in captured["argv"]:
        for needle in _NEEDLES:
            assert needle not in element, (needle, captured["argv"])
    # the message reaches the brain ONLY on stdin (input=), never argv
    assert ATTACK in captured["kw"].get("input", "")
    # the brain runs restricted (no command/code tools, settings ignored) - never bypass/skip-permissions
    assert "--restricted" in captured["argv"]
    assert "bypassPermissions" not in captured["argv"]
    assert "--dangerously-skip-permissions" not in captured["argv"]
    # a normal reply is returned
    assert json.loads(out)["reply"] == "ok"

    # and the message landed in the web-chat lane INBOX as REDACTED <untrusted> DATA
    from twin_agent import session_launch
    inbox = session_launch.lane_folder("web-chat") / "INBOX.md"
    text = inbox.read_text(encoding="utf-8")
    assert "<untrusted" in text and "</untrusted>" in text
    # the attack payload lives ONLY inside the <untrusted> block - it is data, nothing else
    head, _, rest = text.partition("<untrusted")
    body, _, tail = rest.partition("</untrusted>")
    assert "calc.exe" not in head and "calc.exe" not in tail and "calc.exe" in body


def _handler_with_headers(cs, headers: dict):
    """A handler instance with just enough wired to call _is_local_request() - no socket, no server loop."""
    from types import SimpleNamespace
    h = cs.H.__new__(cs.H)
    h.server = SimpleNamespace(server_port=8777)
    h.headers = headers
    return h


def test_ask_endpoint_is_gated_to_the_local_origin():
    """The /ask gate closes the drive-by / DNS-rebinding vector: any page in Principal's browser can reach
    127.0.0.1, but it cannot forge the loopback Host, match the Origin, or send application/json without a
    preflight the server never answers."""
    cs = _load_chat_server()
    port = 8777
    ok_headers = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"}
    # the real UI's request passes (same-origin Origin present)
    assert _handler_with_headers(cs, {**ok_headers, "Origin": f"http://127.0.0.1:{port}"})._is_local_request()
    assert _handler_with_headers(cs, {"Host": f"localhost:{port}", "Content-Type": "application/json"})._is_local_request()
    # every hostile / malformed variant is rejected
    rejected = [
        {"Host": "evil.com", "Content-Type": "application/json"},                       # forged / rebinding host
        {"Host": f"127.0.0.1:{port}", "Content-Type": "text/plain"},                    # no-preflight simple POST
        {"Host": f"127.0.0.1:{port}"},                                                  # missing content-type
        {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json",
         "Origin": "http://evil.com"},                                                  # cross-site origin
        {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json", "Origin": "null"},  # opaque origin
    ]
    for hdrs in rejected:
        assert _handler_with_headers(cs, hdrs)._is_local_request() is False, hdrs


def test_tracked_chat_server_has_no_shell_rce():
    """Static guard on the shipped server: no shell, no string-built .bat spawn, no skip-permissions."""
    src = _CHAT_SERVER.read_text(encoding="utf-8")
    for banned in ("shell=True", ".bat", "dangerously-skip-permissions", "Popen("):
        assert banned not in src, banned
    assert "session_launch" in src and "handle_ask" in src


def test_dangerous_bats_are_deleted_and_no_skip_permissions_in_telegram():
    """session.bat / worker_session.bat were string-built-task + skip-permissions spawns; they are removed and
    no file under telegram/ may re-introduce --dangerously-skip-permissions."""
    tg = _REPO / "telegram"
    assert not (tg / "session.bat").exists()
    assert not (tg / "worker_session.bat").exists()
    for f in tg.iterdir():
        if f.is_file():
            assert "dangerously-skip-permissions" not in f.read_text(encoding="utf-8", errors="ignore"), f.name
