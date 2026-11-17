"""Setup / provision interview: the twin walks Principal through supplying the things it needs to go live.

The person-interview (interview.py) learns WHO Principal is. This learns WHAT he can provide: the credentials,
consents and resources that are the twin's known blockers (see proof.pipeline.KNOWN_BLOCKERS). It:
  - SCANS what is already satisfied (secrets already in the vault, consents already recorded), and
  - asks Principal only for what is still MISSING, storing:
      * secrets -> the encrypted vault (twin_agent.creds, allow-listed names only), never to disk in the clear,
      * consents / resources -> a non-secret status file (data/onboarding/provision.json): a path or a
        "confirmed <date>" marker, never a secret value.

Run it:
    python -I aa.py setup                      # interactive; secret values are read hidden (getpass)
    python -I aa.py setup --status             # just scan + print what's satisfied vs missing (asks nothing)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import creds, paths
from . import wizard


@dataclass(frozen=True)
class Requirement:
    key: str            # id. For a secret, this is the exact vault name (must be in creds.NAMES).
    kind: str           # 'secret' | 'consent' | 'resource'
    label: str
    why: str            # the blocker it clears (traceable to proof KNOWN_BLOCKERS where relevant)
    prompt: str
    required: bool = True
    where: str = ""     # one line: where a non-technical user gets this (the page, the menu)
    # Optional format check for a secret. str -> (ok, plain-English error that never echoes the value).
    validate: Callable[[str], tuple[bool, str]] | None = None


# What Principal provides. Secret keys are real vault names (creds.NAMES); consents/resources are non-secret markers.
REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement("odoo-sys-principal", "secret", "Acme Odoo API key (helpdesk.example.com)",
                "read + work tickets as Principal", 'Paste Principal\'s helpdesk.example.com key as JSON {"login","api_key"}:',
                where="helpdesk.example.com -> your avatar -> Preferences -> Account Security -> New API Key",
                validate=wizard.valid_json_object),
    Requirement("tg-bot-token", "secret", "Telegram bot token (@digital_twin_bot)",
                "the twin's Telegram presence", "Paste the BotFather token for Principal's bot:",
                where="open @BotFather in Telegram -> /newbot (or /token for an existing bot)",
                validate=wizard.valid_bot_token),
    Requirement("wiki", "secret", "Wiki per-bot API key",
                "clears blocker 'wiki_key' (watcher/recorder)", "Paste the Wiki per-bot API key:",
                where="wiki.example.com -> Settings -> API keys (ask your Wiki admin if you can't see it)"),
    Requirement("elevenlabs", "secret", "ElevenLabs API key",
                "proof-video narration in Principal's voice", "Paste the ElevenLabs API key:",
                where="elevenlabs.io -> your profile icon -> API Keys"),
    Requirement("google-oauth-client", "secret", "Google OAuth client (Gmail intake)",
                "email intake", "Paste the Google OAuth client JSON:", required=False,
                where="Google Cloud Console -> APIs & Services -> Credentials -> OAuth client (Desktop app) -> "
                      "Download JSON",
                validate=wizard.valid_json_object),
    Requirement("groq", "secret", "Groq API key (cross-model refuter)",
                "the non-Claude adversarial refuter", "Paste the Groq API key:", required=False,
                where="console.groq.com -> API Keys"),
    Requirement("d2_voice", "consent", "Voice-clone consent + recording",
                "clears blocker 'voice' (ElevenLabs clone)",
                "Do you consent to a voice clone? If yes, give the path to your ~12-min recording "
                "(or press Enter / 'no' to skip):",
                where="a quiet ~12-minute recording of Principal reading anything; save it as a .wav/.mp3 file"),
    Requirement("avatar_consent", "consent", "Avatar consent",
                "his face/name on the bot",
                "Do you consent to your name/face on the bot? Type 'yes' to confirm (or Enter / 'no' to skip):"),
    Requirement("qa_user", "resource", "Non-admin QA user on stagelatest",
                "clears blocker 'qa_user'",
                "Is a non-admin QA user created on stagelatest? If yes, type its username (or Enter / 'no'):"),
    Requirement("shopify_sandboxed", "resource", "Shopify keys sandboxed on stagelatest",
                "clears blocker 'shopify_keys' (LIVE STORE DANGER)",
                "Are stagelatest's live Shopify WRITE keys removed/sandboxed? Type 'confirmed' ONLY if truly done "
                "(else Enter to skip):"),
)


# Work sites the twin signs into on its own. Each login (username + password) goes into the ONE encrypted
# "site-logins" vault entry (JSON {site: {username, password}}, DPAPI), so Digital Twin can fill them itself.
# PREFER API keys / OAuth: sites that already have an API/OAuth path are NOT here and must not store a password.
# - helpdesk.example.com (Odoo): reached by API only (odoo-sys-principal key) and browser-banned (S7) -> dropped.
# - accounts.google.com (Gmail): reached by the Gmail API over OAuth (google-oauth-client + refresh token) -> dropped.
WORK_SITES: tuple[tuple[str, str], ...] = (
    ("www.odoo.sh", "Odoo.sh (staging / dev branches)"),
    ("wiki.example.com", "Wiki (meeting intelligence / watcher)"),
)


def load_site_logins(*, backend: Any = None) -> dict:
    """The {site: {username, password}} map from the vault (empty if unset). Values never printed."""
    if not creds.has_secret("site-logins", backend=backend):
        return {}
    raw = creds._get("site-logins", backend=backend, caller="onboard", purpose="onboard")
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def store_site_login(site: str, username: str, password: str, *, backend: Any = None) -> None:
    """Merge one site login into the encrypted site-logins vault entry (never plaintext on disk)."""
    logins = load_site_logins(backend=backend)
    logins[site] = {"username": username, "password": password}
    creds.set_secret("site-logins", json.dumps(logins), backend=backend, caller="onboard-setup")


def missing_site_logins(*, backend: Any = None) -> list[tuple[str, str]]:
    have = load_site_logins(backend=backend)
    return [(s, label) for s, label in WORK_SITES if s not in have]


def collect_site_logins(ask, *, backend: Any = None) -> list[str]:
    """Ask Principal for each not-yet-stored work-site login; store to the vault. ask(prompt, is_secret)->str."""
    stored: list[str] = []
    for site, label in WORK_SITES:
        if site in load_site_logins(backend=backend):
            continue
        user = (ask(f"{label} - username/email for {site} (Enter to skip):", False) or "").strip()
        if not user:
            continue
        pw = (ask(f"{label} - password for {site} (hidden):", True) or "").strip()
        if not pw:
            continue
        store_site_login(site, user, pw, backend=backend)
        stored.append(site)
    return stored


def cookie_guidance() -> str:
    """Printed to Principal: how to export his logged-in work-site cookies so the twin reuses the sessions."""
    return (
        "Cookie export (so the twin reuses your already-logged-in sessions):\n"
        "  - RECOMMENDED: log in once inside the twin's own 'Digital Twin' browser profile; cookies stay there.\n"
        "  - FALLBACK (allow-listed work domains only, values never printed):\n"
        "      python -I scripts/onboard/import_cookies.py              # dry-run: lists domains + counts\n"
        "      python -I scripts/onboard/import_cookies.py --store --yes # store into the DPAPI vault\n"
        "  Newer app-bound Chrome cookies can't be exported - use the in-browser login path for those."
    )


def status_path() -> Path:
    return paths.data_dir() / "onboarding" / "provision.json"


def load_status(path: Path | None = None) -> dict:
    p = path or status_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_status(status: dict, *, path: Path | None = None) -> Path:
    p = path or status_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def is_satisfied(req: Requirement, *, backend: Any = None, status: dict | None = None) -> bool:
    if req.kind == "secret":
        return creds.has_secret(req.key, backend=backend)
    return bool((status or {}).get(req.key))


def scan(*, backend: Any = None, path: Path | None = None) -> list[tuple[Requirement, bool]]:
    """Each requirement + whether it's already satisfied. Reads only presence, never secret values."""
    status = load_status(path)
    return [(r, is_satisfied(r, backend=backend, status=status)) for r in REQUIREMENTS]


def _accept(req: Requirement, value: str) -> str | None:
    """What (if anything) to store for this answer. None = treat as skipped / not provided.

    Skip words ('no', 'skip', '', ...) count as skip for EVERY kind, including secrets, so a user who
    types 'skip' never lands the literal word in the vault. Two consents are strict on purpose:
      - shopify_sandboxed only counts when the answer is exactly 'confirmed' (LIVE STORE safety), and
      - avatar_consent only counts on an explicit yes."""
    v = (value or "").strip()
    if wizard.is_skip(v):
        return None
    if req.key == "shopify_sandboxed":
        return "confirmed" if v.lower() == "confirmed" else None
    if req.key == "avatar_consent":
        return "yes" if v.lower() in {"yes", "y"} else None
    return v


def _store(req: Requirement, value: str, *, backend: Any, status: dict) -> None:
    """Secret -> vault; consent/resource -> non-secret status marker. Caller passes an accepted value."""
    if req.kind == "secret":
        creds.set_secret(req.key, value, backend=backend, caller="onboard-setup")
    else:
        # never store a secret here; a path or a confirmation marker only
        status[req.key] = value


def run_setup(answers: dict[str, str], *, backend: Any = None, path: Path | None = None) -> dict:
    """Non-interactive core: store any provided answer whose requirement isn't already satisfied.

    An answer that reads as a skip ('no'/'skip'/''), an unconfirmed Shopify answer, or a non-yes avatar
    answer is treated as NOT provided (stays missing) - see _accept."""
    status = load_status(path)
    stored: list[str] = []
    for r in REQUIREMENTS:
        if is_satisfied(r, backend=backend, status=status):
            continue
        accepted = _accept(r, answers.get(r.key, ""))
        if accepted is None:
            continue
        _store(r, accepted, backend=backend, status=status)
        stored.append(r.key)
    save_status(status, path=path)
    scanned = scan(backend=backend, path=path)
    missing_required = [r.key for r, ok in scanned if not ok and r.required]
    return {
        "stored": stored,
        "satisfied": [r.key for r, ok in scanned if ok],
        "missing_required": missing_required,
        "missing_optional": [r.key for r, ok in scanned if not ok and not r.required],
    }


# Login-only secrets that setup can't collect inline: they're earned by running another command once.
# Each: (vault_name, friendly label, the exact command, a prerequisite vault_name or None).
LOGIN_ACTIONS: tuple[tuple[str, str, str, str | None], ...] = (
    ("tg-user-session", "Telegram login (sign in as your own account)",
     "python -I aa.py tg-login", None),
    ("gmail-ticketmail-refresh", "Gmail login (ticketmail inbox)",
     "python -I aa.py gmail-login ticketmail", "google-oauth-client"),
)


def checklist(*, backend: Any = None, path: Path | None = None) -> list[dict]:
    """One flat checklist of everything go-live needs: the asked requirements plus the login-only secrets.

    Each entry: {key, label, ok, required, command}. Reads only presence, never a secret value. ``command``
    is the exact next command for a missing item (login items only; others are filled by re-running setup).
    Gmail's command points first at the OAuth-client step when that prerequisite isn't set yet."""
    status = load_status(path)
    items: list[dict] = []
    for r in REQUIREMENTS:
        items.append({"key": r.key, "label": r.label,
                      "ok": is_satisfied(r, backend=backend, status=status),
                      "required": r.required,
                      "command": None if is_satisfied(r, backend=backend, status=status) else "python -I aa.py setup"})
    for vault_name, label, cmd, prereq in LOGIN_ACTIONS:
        ok = creds.has_secret(vault_name, backend=backend)
        command = None
        if not ok:
            if prereq and not creds.has_secret(prereq, backend=backend):
                command = f"python -I aa.py creds set-file {prereq} <path to client_secret.json>   then:   {cmd}"
            else:
                command = cmd
        items.append({"key": vault_name, "label": label, "ok": ok, "required": False, "command": command})
    return items


def render_checklist(items: list[dict]) -> str:
    """ASCII checklist (safe on a Windows console): [x] done / [ ] still needed, with the next command."""
    lines = ["", "Your Digital Twin setup checklist:"]
    for it in items:
        lines.append(f"  {'[x]' if it['ok'] else '[ ]'} {it['label']}")
        if not it["ok"] and it.get("command"):
            lines.append(f"        next: {it['command']}")
    missing_required = [it["key"] for it in items if not it["ok"] and it["required"]]
    lines.append("")
    if missing_required:
        lines.append("Still needed before go-live: " + ", ".join(missing_required))
    else:
        lines.append("All required items are set - the twin has what it needs to go live.")
    return "\n".join(lines)


def interactive(ask: Callable[[str, bool], str] = None, out=print,
                *, backend: Any = None, path: Path | None = None) -> dict:
    """Walk Principal through each still-missing item with numbered steps, a why+where line, and validation.

    ask(prompt, is_secret) -> value (injectable for tests). Secrets are read hidden, validated, re-asked on
    bad input (bounded), stored encrypted, and never echoed back - acknowledged only as 'saved (encrypted)'.
    Items already in the vault are shown as [have] and never re-asked (idempotent)."""
    if ask is None:
        import getpass
        ask = lambda prompt, is_secret: (getpass.getpass(prompt + " ") if is_secret else input(prompt + " "))
    status = load_status(path)
    out("Digital twin setup. I'll ask only for what I don't already have. Secrets are stored encrypted and")
    out("are never shown back to you - not even to confirm them.\n")

    pending = [r for r in REQUIREMENTS if not is_satisfied(r, backend=backend, status=status)]
    total = len(pending)
    for r in REQUIREMENTS:
        if is_satisfied(r, backend=backend, status=status):
            out(f"[have] {r.label}")
    answers: dict[str, str] = {}
    for i, r in enumerate(pending, 1):
        out(f"\n{wizard.step_line(i, total, r.label)}")
        out(f"  Why: {r.why}")
        if r.where:
            out(f"  Where: {r.where}")
        is_secret = r.kind == "secret"
        value = wizard.ask_value(lambda text: ask(text, is_secret), r.prompt,
                                 optional=not r.required, validate=r.validate, say=out)
        answers[r.key] = value
        if value and is_secret:
            out("  saved (encrypted, never shown)")

    summary = run_setup(answers, backend=backend, path=path)
    out(f"\nStored {len(summary['stored'])} new item(s). Still missing (required): "
        f"{', '.join(summary['missing_required']) or 'none - ready'}.")

    # Work-site logins so Digital Twin can spawn its browser and sign in on its own, then cookie-export guidance.
    site_stored = collect_site_logins(ask, backend=backend)
    if site_stored:
        out(f"Stored work-site logins for: {', '.join(site_stored)} (encrypted vault - the twin signs in itself).")
    out("\n" + cookie_guidance())
    out(render_checklist(checklist(backend=backend, path=path)))
    summary["site_logins_stored"] = site_stored
    return summary


def main(argv: list[str] | None = None, *, backend: Any = None) -> int:
    import argparse
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="setup", description="Provision interview: what Principal supplies for go-live.")
    ap.add_argument("--status", action="store_true", help="just scan + print satisfied vs missing; ask nothing")
    ap.add_argument("--plan", action="store_true",
                    help="output the setup checklist as JSON (key/label/ok/required/command) for a setup-agent "
                         "to drive the steps; reads presence only, never a secret value")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if a.plan:
        import json as _json
        items = checklist(backend=backend)
        print(_json.dumps({"items": items,
                           "missing_required": [it["key"] for it in items if not it["ok"] and it["required"]],
                           "note": "Each secret is entered by the user and stored encrypted; values are never "
                                   "read or harvested. Run each 'command' in order; re-run `setup` for the rest."},
                          ensure_ascii=False, indent=2))
        return 0
    if a.status:
        print(render_checklist(checklist(backend=backend)))
        return 0
    interactive(backend=backend)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
