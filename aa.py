"""One entry point for every command, safe under `python -I` (CONTRACT section 15).

`python -I` does not put the script's folder on sys.path, so we add this repo folder explicitly
(and nothing else: not the current folder, not PYTHONPATH). Usage:

    python -I aa.py <command> [args...]
    commands: creds, listener, kb-build, kb-search, replay, board, guards-scan, tg-login, gmail-login,
              e2e-dry-run, mcp-kb-search, mcp-submit-packet, mcp-tg-history, mcp-odoo-client-read, watcher, voice, whatsapp,
              monitor, helpdesk, timesheets, browser, memory, onboard, setup
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent

COMMANDS = {
    "creds": "twin_agent.creds",
    "listener": "twin_agent.listener",
    "kb-build": "twin_agent.kb.kb_build",
    "kb-search": "twin_agent.kb.kb_search",
    "replay": "twin_agent.replay.replay",
    "e2e-dry-run": "twin_agent.replay.e2e",
    "board": "twin_agent.board.server",
    "guards-scan": "twin_agent.checks.guards",
    "tg-login": "scripts.tg_login",
    "gmail-login": "scripts.gmail_login",
    "mcp-kb-search": "twin_agent.kb.mcp_kb_search",
    "mcp-submit-packet": "twin_agent.worker.mcp_submit_packet",
    "mcp-tg-history": "twin_agent.channels.mcp_tg_history",
    "mcp-odoo-client-read": "twin_agent.channels.mcp_odoo_client_read",
    "watcher": "twin_agent.watcher.cli",
    "voice": "twin_agent.voice.cli",
    "whatsapp": "twin_agent.channels.whatsapp_cli",
    "monitor": "twin_agent.monitor.cli",
    "helpdesk": "twin_agent.helpdesk.cli",
    "timesheets": "twin_agent.timesheets.cli",
    "browser": "twin_agent.browser.cli",
    "memory": "twin_agent.memory.cli",
    "onboard": "twin_agent.onboarding.interview",
    "setup": "twin_agent.onboarding.setup",
    "set-automation": "twin_agent.onboarding.automation",
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in COMMANDS:
        print(__doc__.strip())
        return 0 if (not argv or argv[0] in ("-h", "--help")) else 2
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    module = importlib.import_module(COMMANDS[argv[0]])
    if not hasattr(module, "main"):
        print(f"{argv[0]}: module has no main() yet", file=sys.stderr)
        return 2
    return int(module.main(argv[1:]) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
