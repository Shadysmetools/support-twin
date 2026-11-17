"""HTML rendering. Every dynamic value goes through ``esc`` (quotes included, so attributes are safe too)."""
from __future__ import annotations

from . import data
from .config import AdminConfig
from .web import esc

NAV = (("/tickets", "Tickets"), ("/numbers", "Connected numbers"), ("/templates", "Templates"),
       ("/connect", "Connect WhatsApp"), ("/holds", "Holds"))

FLASH = {
    "sent": ("ok", "Approved. The hub re-checked the approval, the draft and the 24-hour window, then sent it."),
    "done": ("ok", "Approved. This step sends nothing to the customer."),
    "not_authorised": ("bad", "You are not an approver for this ticket. Nothing was sent."),
    "stale": ("bad", "That draft changed or was already handled. Reload and check the current draft. Nothing was sent."),
    "window_closed": ("bad", "Approved but NOT sent: the customer's 24-hour window is closed."),
    "edited_text_flagged": ("bad", "Your edited text was flagged by the checker and was NOT sent. Review it."),
    "edit_contains_sensitive_data": ("bad", "Your edit contains sensitive data and was NOT sent."),
    "placeholder_left": ("bad", "An unfilled [[placeholder]] remains. Nothing was sent."),
    "send_unknown": ("bad", "The send did not confirm. Check WhatsApp. It will NOT be resent automatically."),
    "send_outcome_unknown": ("bad", "An earlier send on this ticket did not confirm. Nothing more was sent."),
    "empty_edit": ("bad", "The edited text is empty. Nothing was sent."),
    "empty_text": ("bad", "The text is empty. Nothing was sent."),
    "csrf": ("bad", "The form expired. Reload the page and try again."),
    "action_failed": ("bad", "Something failed after approval. Check the ticket and WhatsApp before retrying: the "
                             "message may already have gone out."),
    "failed": ("bad", "Not completed. Check the ticket and WhatsApp before retrying."),
    "meta_ok": ("ok", "Done. Meta accepted the request. Reload to see the new status."),
    "meta_disabled": ("bad", "Meta write actions are switched off on this server. Nothing was changed in Meta."),
    "meta_bad": ("bad", "That request was not valid. Nothing was changed in Meta."),
    "meta_failed": ("bad", "Meta did not confirm the request. Check in Meta before trying again. It is not retried "
                           "automatically."),
    "meta_not_connected": ("bad", "Refused here, before anything was sent to Meta: that account or number is not connected "
                                  "(pick the number first). Nothing was changed in Meta."),
    "meta_refused": ("bad", "Meta refused the request. Nothing was changed in Meta."),
    "picked": ("ok", "Number selected. You can now start the history sync for it."),
    "pick_bad": ("bad", "That number could not be selected. Nothing was changed."),
    "connect_busy": ("bad", "Too many sign-ups are waiting. Try again in a few minutes."),
    "connect_off": ("bad", "Embedded Signup is not configured on this server."),
    "connect_bad": ("bad", "The business name must be 1-40 letters, digits or dashes."),
}


def layout(title: str, body: str, *, cfg: AdminConfig, user=None, csrf: str = "", active: str = "",
           flash: str = "") -> str:
    nav = ""
    if user:
        links = "".join(f'<a href="{p}"{" class=on" if p == active else ""}>{esc(t)}</a>' for p, t in NAV)
        nav = (f'<nav>{links}<span class="who">{esc(user.display_name)}'
               f'<form method="post" action="/logout" class="inline"><input type="hidden" name="csrf" value="{esc(csrf)}">'
               '<button class="link">Log out</button></form></span></nav>')
    banner = f'<div class="banner">{esc(cfg.banner)}</div>' if cfg.banner else ""
    return ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{esc(title)} - Support Twin</title><link rel=\"stylesheet\" href=\"/static/admin.css\"></head><body>"
            f"{banner}<header><strong>Support Twin</strong> <span class=\"sub\">by Acme: AI drafts, people approve</span>"
            f"</header>{nav}<main><h1>{esc(title)}</h1>{_flash(flash)}{body}</main></body></html>")


def _flash(code: str) -> str:
    if not code:
        return ""
    cls, text = FLASH.get(code, FLASH["failed"])
    return f'<p class="flash {cls}" role="status">{esc(text)}</p>'


def login_page(cfg: AdminConfig, pre_token: str, error: str = "") -> str:
    err = f'<p class="flash bad">{esc(error)}</p>' if error else ""
    body = (f'{err}<form method="post" action="/login" class="card">'
            f'<input type="hidden" name="csrf" value="{esc(pre_token)}">'
            '<label>Username <input name="username" autocomplete="username" required autofocus></label>'
            '<p class="hint">Your username (e.g. sysadmin) or your work email</p>'
            '<label>Password <input name="password" type="password" autocomplete="current-password" required></label>'
            '<button>Log in</button></form>')
    return layout("Log in", body, cfg=cfg)


def connect_page(cfg: AdminConfig, user, csrf: str, businesses: list[dict] | None = None, *, ready: bool = False,
                 flash: str = "") -> str:
    """No Facebook SDK here: the launcher runs on its OWN origin (``cfg.launcher_url``). This page only mints the
    single-use state and sends the browser there."""
    note = "" if ready else ('<p class="flash bad">Embedded Signup is not configured on this server (app id, configuration '
                             'id or launcher address is missing), so the button is disabled.</p>')
    dis = "" if ready else " disabled"
    rows = "".join(f"<tr><td>{esc(b['business'])}</td><td>{'yes' if b['token_stored'] else 'no'}</td>"
                   f"<td>{esc(b['last_event'] or '-')}</td><td>{esc(b['updated'] or '-')}</td></tr>" for b in businesses or [])
    table = ("<h2>Connected businesses</h2><table><thead><tr><th>Business</th><th>Access stored</th><th>Last event</th>"
             f"<th>Updated</th></tr></thead><tbody>{rows}</tbody></table>") if rows else ""
    body = (f"{note}<p>Connect a WhatsApp Business number through Meta Embedded Signup. You choose the business, the "
            "WhatsApp Business Account and the number. You can keep using the WhatsApp Business app on the same number "
            "(coexistence). The sign-up window opens on a separate address, so Meta's script never runs on this page.</p>"
            f'<form method="post" action="/connect/start" class="card"><input type="hidden" name="csrf" value="{esc(csrf)}">'
            '<label>Business name <input name="business" value="default" pattern="[a-z0-9][a-z0-9-]{0,39}" required></label>'
            f"<button{dis}>Connect WhatsApp</button></form>"
            '<p class="muted">After you finish in Meta, open <a href="/numbers">Connected numbers</a>. Connecting does '
            "not subscribe webhooks, register the number or start a data sync.</p>" + table)
    return layout("Connect WhatsApp", body, cfg=cfg, user=user, csrf=csrf, active="/connect", flash=flash)


def _write_forms(cfg: AdminConfig, csrf: str, w: dict, n: dict) -> str:
    """The explicit Meta write actions. Disabled (and refused server-side) unless ``allow_meta_writes``. The history sync
    is offered only for the SELECTED number of the account; the other numbers get a local "Use this number" form."""
    dis = "" if cfg.allow_meta_writes else " disabled"
    hid = (f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
           f'<input type="hidden" name="business" value="{esc(w.get("business"))}">')
    sub = (f'<form method="post" action="/meta/subscribe" class="inline">{hid}<input type="hidden" name="waba_id" '
           f'value="{esc(w.get("id"))}"><label><input type="checkbox" name="confirm" value="yes"{dis}> confirm</label> '
           f'<button{dis}>Subscribe webhooks</button></form>')
    if n.get("id") != w.get("selected_phone"):
        return sub + (f'<form method="post" action="/meta/pick" class="inline">{hid}<input type="hidden" '
                      f'name="phone_number_id" value="{esc(n.get("id"))}"><button>Use this number for the sync</button></form>'
                      '<span class="muted"> number not selected: the history sync is off for it</span>')
    sync = "".join(
        f'<form method="post" action="/meta/sync" class="inline">{hid}<input type="hidden" name="phone_number_id" '
        f'value="{esc(n.get("id"))}"><input type="hidden" name="sync_type" value="{t}">'
        f'<label><input type="checkbox" name="confirm" value="yes"{dis}> confirm</label> <button{dis}>{label}</button></form>'
        for t, label in (("history", "Start history sync"), ("smb_app_state_sync", "Start contacts sync")))
    return sub + sync


def numbers_page(cfg, user, csrf, wabas: list[dict] | None, error: str = "", flash: str = "") -> str:
    if error:
        body = f'<p class="flash bad">{esc(error)}</p>'
    elif not wabas:
        body = '<p class="muted">No WhatsApp Business Account is connected yet. Use Connect WhatsApp.</p>'
    else:
        rows = ""
        for w in wabas:
            if w.get("error"):
                rows += (f"<tr><td>{esc(w.get('name'))}</td><td colspan=\"4\"><span class=\"tag warn\">Could not read this "
                         f"business: {esc(w['error'])}</span></td></tr>")
            for n in w.get("phone_numbers", []):
                subs = "Webhooks: subscribed" if n.get("webhooks_subscribed") else "Webhooks: NOT subscribed"
                acts = '<span class="muted">Read-only (our own number)</span>' if w.get("read_only") else _write_forms(cfg, csrf, w, n)
                rows += (f"<tr><td>{esc(w.get('name'))}</td><td>{esc(data.mask_number(n.get('display_phone_number', '')))}"
                         f"</td><td>{esc(n.get('status'))} (quality {esc(n.get('quality_rating'))})</td>"
                         f"<td>{esc(subs)}</td><td>{acts}</td></tr>")
        off = "" if cfg.allow_meta_writes else (
            '<p class="flash bad">The actions below are switched off on this server (<code>allow_meta_writes</code>). '
            "They change things in Meta and need the owner's OK first. Coexistence: the history sync must start within "
            "24 hours of connecting, or the business has to redo the sign-up.</p>")
        body = (f"{off}<table><thead><tr><th>WhatsApp Business Account</th><th>Number</th><th>Status</th><th>Webhooks</th>"
                f"<th>Actions (change Meta)</th></tr></thead><tbody>{rows}</tbody></table>")
    return layout("Connected numbers", body, cfg=cfg, user=user, csrf=csrf, active="/numbers", flash=flash)


def templates_page(cfg, user, csrf, items: list[dict] | None, error: str = "") -> str:
    note = f'<p class="flash bad">{esc(error)}</p>' if error else ""
    if not items:
        body = note or '<p class="muted">No approved templates.</p>'
    else:
        rows = "".join(f"<tr><td>{esc(t.get('waba'))}</td><td>{esc(t.get('name'))}</td><td>{esc(t.get('language'))}</td>"
                       f"<td>{esc(t.get('category'))}</td><td>{esc(t.get('status'))}</td><td>{esc(t.get('body'))}</td></tr>"
                       for t in items)
        body = ("<p class=\"muted\">Approved templates, read through the Graph API. Outside the 24-hour window only an "
                "approved template can be used.</p><table><thead><tr><th>Account</th><th>Name</th><th>Language</th>"
                f"<th>Category</th><th>Status</th><th>Text</th></tr></thead><tbody>{rows}</tbody></table>")
        body = note + body
    return layout("Templates", body, cfg=cfg, user=user, csrf=csrf, active="/templates")


def tickets_page(cfg, user, csrf, rows: list[dict]) -> str:
    if not rows:
        return layout("Tickets", '<p class="muted">No tickets yet.</p>', cfg=cfg, user=user, csrf=csrf, active="/tickets")
    trs = "".join(f'<tr><td><a href="/tickets/{int(r["tid"])}">#{int(r["tid"])}</a></td><td>{esc(r["customer"])}</td>'
                  f'<td>{esc(r["number"])}</td><td>{esc(r["kind"])}</td><td>{esc(r["status"])}</td>'
                  f'<td>{esc(r["delivery"] or "-")}</td></tr>' for r in rows)
    body = ("<table><thead><tr><th>Ticket</th><th>Customer</th><th>Number</th><th>Type</th><th>Status</th>"
            f"<th>Delivery</th></tr></thead><tbody>{trs}</tbody></table>")
    return layout("Tickets", body, cfg=cfg, user=user, csrf=csrf, active="/tickets")


def holds_page(cfg, user, csrf, rows: list[dict]) -> str:
    """Open holds: messages a person must look at. No client text: a label, the last 4 digits, the ticket if there is one."""
    if not rows:
        return layout("Holds", '<p class="muted">No open holds.</p>', cfg=cfg, user=user, csrf=csrf, active="/holds")
    trs = "".join(f'<tr><td>{esc(r["id"])}</td><td>{esc(r["label"])}</td><td>{esc(r["sender"])}</td>'
                  + (f'<td><a href="/tickets/{int(r["ticket_id"])}">#{int(r["ticket_id"])}</a></td>' if r["ticket_id"] else "<td>-</td>")
                  + f'<td>{esc(r["created"])}</td><td><form method="post" action="/holds/{esc(r["id"])}/done" class="inline">'
                  f'<input type="hidden" name="csrf" value="{esc(csrf)}"><button>Done</button></form></td></tr>' for r in rows)
    body = ("<table><thead><tr><th>Hold</th><th>What happened</th><th>Number</th><th>Ticket</th><th>When</th><th></th></tr></thead>"
            f"<tbody>{trs}</tbody></table>")
    return layout("Holds", body, cfg=cfg, user=user, csrf=csrf, active="/holds")


def _judge_badge(dr: dict) -> str:
    """The same verdict the Telegram approver sees: judge score, plus LOW SCORE / UNJUDGED (owner-review)."""
    if dr.get("judge_unjudged"):
        return ' <span class="tag warn">UNJUDGED: owner-review</span>'
    if dr.get("judge") is None:
        return ""
    cls, extra = ("tag warn", " | LOW SCORE: owner-review") if dr.get("judge_flag") else ("tag", "")
    return f' <span class="{cls}">judge {esc(dr["judge"])}{esc(extra)}</span>'


def _draft_block(d: dict, csrf: str, tid: int) -> str:
    dr = d["draft"]
    if not dr:
        return '<p class="muted">No draft is waiting for approval.</p>'
    hidden = (f'<input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="nonce" '
              f'value="{esc(dr["nonce"])}"><input type="hidden" name="draft_hash" value="{esc(dr["draft_hash"])}">')
    label = esc(data.KIND_LABEL.get(dr["kind"], dr["kind"])) + _judge_badge(dr)
    if not d["editable"]:
        return (f'<div class="draft"><span class="tag">Awaiting approval</span> <b>{label}</b><pre>{esc(dr["draft"])}</pre>'
                f'<form method="post" action="/tickets/{tid}/approve">{hidden}'
                '<button name="action" value="approve">Approve</button></form></div>')
    return (f'<div class="draft"><span class="tag">Awaiting approval</span> <b>{label}</b>'
            f'<p class="muted">AI draft. Nothing is sent until a team member approves.</p>'
            f'<form method="post" action="/tickets/{tid}/approve">{hidden}'
            f'<textarea name="text" rows="7" required>{esc(dr["draft"])}</textarea>'
            '<div class="row"><button name="action" value="edit_approve">Edit + Approve</button> '
            '<button name="action" value="approve" class="plain" formnovalidate>Approve as drafted</button></div></form></div>')


def ticket_page(cfg, user, csrf, d: dict, flash: str = "") -> str:
    tid = int(d["tid"])
    msg = f"<blockquote>{esc(d['message'])}</blockquote>"
    if d["transcript"]:
        sim = " (placeholder transcript)" if d["transcript_placeholder"] else ""
        msg = f'<p class="muted">Voice note transcript{esc(sim)}</p>{msg}'
    more = "".join(f'<blockquote>{esc(r.get("text"))} <span class="muted">({esc(r.get("kind"))} reply)</span></blockquote>'
                   for r in d["replies"])
    tl = "".join(f"<li><span class=\"ts\">{esc(ts[:19].replace('T', ' ') + ' UTC' if ts else 'time not recorded')}</span> "
                 f"{esc(label)}</li>" for ts, label in d["timeline"])
    body = (f'<p>{esc(d["customer"])} ({esc(d["number"])}) at <b>{esc(d["client"])}</b>. Status: <b>{esc(d["status"])}</b>. '
            f'Delivery: <b>{esc(d["delivery"] or "-")}</b>. Owner: {esc(d["owner"] or "-")}.</p>'
            f"<h2>Customer message</h2>{msg}{more}<h2>Reply</h2>{_draft_block(d, csrf, tid)}"
            f'<h2>Timeline</h2><ol class="timeline">{tl}</ol><p><a href="/tickets">Back to tickets</a></p>')
    return layout(f"Ticket #{tid}", body, cfg=cfg, user=user, csrf=csrf, active="/tickets", flash=flash)
