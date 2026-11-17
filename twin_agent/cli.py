"""`support-twin <command> [args...]`: the installed entry point (same commands as `python -I aa.py`).

Loads ./.env first (existing environment variables win), then dispatches to the command's module.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

COMMANDS = {
    "demo": ("twin_agent.demo", "one synthetic ticket end to end, every external system faked"),
    "doctor": ("twin_agent.cli", "show the resolved home, profile and config"),
    "listener": ("twin_agent.listener", "the main process (dry_run until you switch it to live)"),
    "creds": ("twin_agent.creds", "store / list secrets in the local vault (names only are ever shown)"),
    "guards-scan": ("twin_agent.checks.guards", "guard self-test (--selftest) before enabling sends"),
    "kb-build": ("twin_agent.kb.kb_build", "build the knowledge base from your exports"),
    "kb-search": ("twin_agent.kb.kb_search", "search the knowledge base"),
    "replay": ("twin_agent.replay.replay", "replay an exported chat through the router and timers"),
    "e2e-dry-run": ("twin_agent.replay.e2e", "an exported chat through the whole listener, never sends"),
    "board": ("twin_agent.board.server", "the local approval board"),
    "helpdesk": ("twin_agent.helpdesk.cli", "helpdesk (Odoo) desk commands"),
    "memory": ("twin_agent.memory.cli", "the case / system-map memory"),
    "monitor": ("twin_agent.monitor.cli", "the twin monitor (flight recorder + auditor)"),
    "watcher": ("twin_agent.watcher.cli", "learn how the principal works (fingerprint)"),
    "timesheets": ("twin_agent.timesheets.cli", "descriptive timesheets from real active time"),
    "voice": ("twin_agent.voice.cli", "voice-note replies (consent required)"),
    "whatsapp": ("twin_agent.channels.whatsapp_cli", "WhatsApp Cloud API tools"),
    "browser": ("twin_agent.browser.cli", "the dedicated, sandboxed browser profile"),
    "onboard": ("twin_agent.onboarding.interview", "interview the principal to build the persona"),
    "setup": ("twin_agent.onboarding.setup", "guided setup of the integrations"),
    "mcp-kb-search": ("twin_agent.kb.mcp_kb_search", "MCP server: knowledge-base search for workers"),
    "mcp-submit-packet": ("twin_agent.worker.mcp_submit_packet", "MCP server: workers submit their packet"),
    "mcp-tg-history": ("twin_agent.channels.mcp_tg_history", "MCP server: read-only Telegram history"),
    "mcp-odoo-read": ("twin_agent.channels.mcp_odoo_client_read", "MCP server: read-only Odoo API for workers"),
}


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            if key == "TWIN_AGENT_HOME" and val:
                val = str(Path(val).resolve())
            os.environ[key] = val


def doctor() -> int:
    from . import paths, profile
    print(f"home:    {paths.home()}")
    print(f"config:  {paths.config_dir() / 'config.yaml'}  "
          f"({'found' if (paths.config_dir() / 'config.yaml').is_file() else 'missing: run the install script'})")
    for k, v in profile.load().items():
        print(f"profile.{k}: {v}")
    return 0


def usage() -> str:
    lines = ["usage: support-twin <command> [args...]", "", "commands:"]
    lines += [f"  {name:<18} {desc}" for name, (_m, desc) in COMMANDS.items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    load_dotenv()
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in COMMANDS:
        print(usage())
        return 0 if (not argv or argv[0] in ("-h", "--help")) else 2
    if argv[0] == "doctor":
        return doctor()
    module = importlib.import_module(COMMANDS[argv[0]][0])
    return int(module.main(argv[1:]) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
