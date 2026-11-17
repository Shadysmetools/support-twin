"""The routed-ticket email to Principal and the Claude Code session it opens (Principal 2026-11-12).

The email is a comment on the Acme ticket to Principal's partner. It carries the status, the request, the analysis
and an "Open as a session in Claude Code" link. Gmail and Odoo's sanitizer may drop a custom `claude://` href, so
the link is plain http to the twin's own server on 127.0.0.1 (`open_server`). That server answers with the
`claude://code/new?q=<prompt>&folder=<hub>` deep link, which pre-fills the prompt; Principal presses Enter.
"""
from __future__ import annotations

import html
from urllib.parse import quote

HUB = r"C:\twin\hub"
TWIN_REPO = r"C:\twin\support-twin"
VENV_PY = r"C:\TwinAgent\.venv\Scripts\python.exe"
PROMPT_MAX = 1800            # the whole deep link stays well under Windows' URL-handler limits


def _e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _clip(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def skill_for(routing) -> str:
    return "/internal-developer" if routing.track == "development" else "/functional-consultant"


def session_prompt(tid: int, rid: str, req: dict, routing, packet: dict, message: str) -> str:
    """The prompt the email's link pre-fills in a new Claude Code session."""
    draft = ((packet or {}).get("draft_reply") or {}).get("text") or ""
    where = " / ".join(x for x in (req.get("chat") or "", req.get("topic") or "") if x)
    lines = [
        f"{skill_for(routing)} ticket #{tid}",
        "",
        f"Opened from the Digital Twin email for Acme ticket #{tid} ({rid}).",
        f"Request from {req.get('requester_name') or 'the requester'}" + (f" in {where}" if where else "") + ":",
        _clip(message, 500),
        "",
        f"Twin analysis: {_clip((packet or {}).get('analysis') or (packet or {}).get('understanding'), 400)}",
        f"Ticket Analyst: {routing.track} / {routing.type_name}, confidence {routing.confidence} ({routing.level})"
        + (" - unsure, check it first" if routing.unsure else ""),
    ]
    if draft:
        lines.append(f"Twin's draft reply: {_clip(draft, 300)}")
    lines += [
        "",
        "Rules:",
        "- Globex live and stagelatest: read only, through the API.",
        f"- Prepare the reply to the requester. Post it only on my yes, with this command run in {TWIN_REPO} "
        f'(no -I): {VENV_PY} -m twin_agent.channels.claude_inbox edit "<reply>" {rid}',
        "- Client email: a Gmail draft only, never sent.",
    ]
    text = "\n".join(lines)
    return text if len(text) <= PROMPT_MAX else text[: PROMPT_MAX - 1] + "…"


def claude_link(prompt: str, folder: str = HUB) -> str:
    return f"claude://code/new?q={quote(prompt, safe='')}&folder={quote(folder, safe='')}"


def email_html(tid: int, rid: str, req: dict, routing, packet: dict, message: str, open_url: str,
               ticket_url: str | None = None) -> str:
    packet = packet or {}
    analysis = packet.get("analysis") or packet.get("understanding") or "(no analysis yet)"
    draft = (packet.get("draft_reply") or {}).get("text") or ""
    where = " / ".join(x for x in (req.get("chat") or "", req.get("topic") or "") if x)
    link = f"{open_url.rstrip('/')}/{int(tid)}"
    # Principal 2026-11-12: the orange button sits right UNDER Odoo's blue "View Helpdesk Ticket" (that one already
    # opens the ticket, so no second Odoo link), same markup and size, same brightness: Odoo's is #5a6ed3
    # (HSL 230/59/59), this is HSL 25/59/59. Inline styles: mail clients drop <style> blocks, and Odoo's
    # sanitizer drops the `background:` shorthand (preview 2, 12 Nov), so background-color, on the cell AND the link.
    button = (f'<table cellspacing="0" cellpadding="0" border="0" style="margin: 0 0 4px 0;"><tbody><tr>'
              f'<td style="border-radius: 3px; text-align: center; background-color: #d48c59;">'
              f'<a href="{_e(link)}" style="font-size: 12px; color: #FFFFFF; display: block; padding: 8px 12px 11px; '
              f'text-decoration: none; font-weight: bold; background-color: #d48c59; border-radius: 3px;">'
              f'Open as a session in Claude Code</a></td>'
              f'<td>&nbsp;&nbsp;</td><td style="font-size: 12px; color: #888888;">{_e(skill_for(routing))} on '
              f"Principal's PC, while the twin runs</td></tr></tbody></table>")
    parts = [
        button,
        f"<p><b>Ticket #{int(tid)} routed to {_e(routing.stage_name)}</b> ({_e(rid)})</p>",
        f"<p><b>Status:</b> {_e(routing.stage_name)}<br/>"
        f"<b>Track / type:</b> {_e(routing.track.title())} / {_e(routing.type_name)}<br/>"
        f"<b>Confidence:</b> {routing.confidence} ({_e(routing.level)})</p>",
    ]
    if routing.unsure:
        why = "; ".join(routing.reasons) or "the checked evidence is weak"
        parts.append(f"<p><b>Unsure, please check:</b> {_e(why)}</p>")
    parts += [
        f"<p><b>Request</b> from {_e(req.get('requester_name') or 'the requester')}"
        + (f" in {_e(where)}" if where else "") + f":<br/>{_e(message)}</p>",
        f"<p><b>Analysis:</b> {_e(analysis)}</p>",
    ]
    qs = packet.get("questions_for_principal") or []
    if qs:
        parts.append(f"<p><b>Check first:</b> {_e(qs[0])}</p>")
    if draft:
        parts.append(f"<p><b>Draft reply (posted only on your yes):</b><br/>{_e(draft)}</p>")
    return "".join(parts)
