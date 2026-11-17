"""Watch Odoo helpdesk tickets and give each one ONE Teamhub thread (rules: ``docs/TEAMHUB-TICKET-THREAD-RULES.md``).

READ-ONLY on Odoo (an ``OdooAdapter`` with writes off, so ``guard.check`` still runs on every read). For each new ticket
matching the configured filter: open the thread, post three role replies built from the EXISTING deterministic stages
(``analyse`` / ``templates`` / ``routing``; no LLM), then, for a ``TEST ·`` ticket only, the Approve / Edit / Reject card.
When a tracked ticket reaches a closed stage: a ``[Closed]`` reply, the root marked ``[CLOSED]``, the thread muted.

PRIVACY: a real ticket (name not starting ``TEST ·``) never shows client text. Its thread gets structured facts only
(type, module, language, urgency) and NO approval card, because an approver must see the exact text and that text
holds the client's words. Those approvals stay in the hub's Telegram flow.

No filter configured = refuse to run (fail closed). A watermark plus ``ThreadMap`` make every run idempotent.
"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime

from ..helpdesk import guard
from ..helpdesk.odoo import JsonRpcTransport, OdooAdapter
from . import templates
from .analyse import analyse
from .teamhub_threads import (ConfigError, Ctx, Settings, card_op, create_op, draft_op, is_test_name, queue_for, reply_op,
                            route_for, stage_flag)
from .model import detect_lang
from .routing import RuleAssigner, default_team
from .slots import client_message

FIELDS = ["name", "partner_id", "description", "user_id", "stage_id", "team_id"]
DOT = "·"


def open_reader(st: Settings) -> OdooAdapter:
    """A read-only Odoo adapter. The key comes from the secrets file named in config and never leaves the transport."""
    path = os.path.expanduser(st.odoo_secret_file)
    if not st.odoo_secret_file or not os.path.exists(path):
        raise ConfigError(f"teamhub_tickets.odoo_secret_file is not set or not found")
    with open(path, encoding="utf-8") as f:
        s = json.load(f)
    t = JsonRpcTransport(str(s["url"]), str(s["db"]), str(s["login"]), str(s["api_key"]))
    return OdooAdapter(t, writes_on=False)


def domain_from(flt: dict) -> list:
    """The ticket filter as an Odoo domain. Refuses when nothing is configured (never 'watch everything')."""
    dom = []
    if (flt or {}).get("team_id"):
        dom.append(["team_id", "=", int(flt["team_id"])])
    if (flt or {}).get("name_like"):
        dom.append(["name", "=like", str(flt["name_like"])])
    if not dom:
        raise ConfigError("teamhub_tickets.filter is empty: refusing to watch every ticket")
    return dom


def closed_stages(odoo) -> dict[int, str]:
    """{stage id: name} of closing stages, by the ONE shared rule (``stage_flag``: fold first, else is_close)."""
    flag = stage_flag(odoo.fields_get("helpdesk.stage"))
    rows = odoo.search_read("helpdesk.stage", [[flag, "=", True]], ["name"])
    return {int(r["id"]): str(r.get("name") or "") for r in rows}


def _m2o(v):
    return (int(v[0]), str(v[1])) if isinstance(v, (list, tuple)) and v else (int(v), "") if v else None


def plain_text(html_text: str) -> str:
    t = re.sub(r"<(br|/p|/li|/div)[^>]*>", "\n", str(html_text or ""))
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"[ \t]+", " ", re.sub(r"\s*\n\s*", "\n", t)).strip()[:1500]


def is_test_contact(st: Settings, p: dict | None) -> bool:
    """The TEST contact: the configured NON-ZERO ``test_partner_id`` (fail closed without it), the exact TEST name, and
    no email, phone or mobile."""
    if not p or p.get("name") != guard.TEST_PARTNER or p.get("email") or p.get("phone") or p.get("mobile"):
        return False
    return bool(st.test_partner_id) and int(p["id"]) == int(st.test_partner_id)


def ticket_views(odoo, rows: list[dict], st: Settings) -> list[dict]:
    """Normalised tickets. ``is_test`` (full client text, a card) needs a TEST name AND the live-read TEST contact:
    a subject or a rename alone is never enough."""
    pids = sorted({_m2o(r.get("partner_id"))[0] for r in rows if _m2o(r.get("partner_id"))})
    people = {int(p["id"]): p for p in odoo.search_read(
        "res.partner", [["id", "in", pids]], ["name", "is_company", "email", "phone", "mobile"])} if pids else {}
    out = []
    for r in rows:
        pid = (_m2o(r.get("partner_id")) or (0, ""))[0]
        p = people.get(pid, {})
        test = is_test_name(r.get("name")) and is_test_contact(st, p)
        client = p.get("name") if p and (test or p.get("is_company")) else (f"Client #{pid}" if pid else "Unknown client")
        out.append({"id": int(r["id"]), "name": str(r.get("name") or ""), "client": client or "Unknown client",
                    "text": plain_text(r.get("description")), "owner": _m2o(r.get("user_id")),
                    "stage_id": (_m2o(r.get("stage_id")) or (0, ""))[0], "is_test": test})
    return out


def ticket_link(ctx: Ctx, tid: int) -> str:
    if ctx.st.odoo_link_template:
        return ctx.st.odoo_link_template.format(tid=tid)
    base = str(getattr(getattr(ctx.odoo, "t", None), "url", "") or "")
    return f"{base}/web#id={tid}&model=helpdesk.ticket&view_type=form" if base else "(no Odoo link configured)"


# ------------------------------------------------------------------------------------------------ the role steps

def approver_for(st: Settings, v: dict) -> str:
    owner = v["owner"][0] if v["owner"] else 0
    return ((route_for(st, owner) or {}).get("approver") or st.owner_teamhub_ids.get(str(owner))
            or st.default_approver or "")


def build_steps(st: Settings, v: dict, link: str) -> dict:
    """Root text + the three role replies + (TEST only) the reply draft. Deterministic: no model is called."""
    msg = client_message(v["text"]) or v["text"]     # the client's own words, not the description's section labels
    a = analyse(msg)
    lang = "Arabic" if detect_lang(msg) == "ar" else "English"
    module = a.module if a.module != "unknown" else "not identified"
    safe = f"{a.ticket_type} ({a.nature}) about {module}; language {lang}; urgency {a.urgency}"
    root = f"Ticket #{v['id']} {DOT} {v['client']} {DOT} source: Odoo\nOdoo: {link}\n"
    root += f"Original message:" f"\n{v['text']}" if v["is_test"] else f"Summary: {safe}"
    if st.admin_link_template and not v["is_test"]:
        root += f"\nApprove the reply in the hub: {st.admin_link_template.format(tid=v['id'])}"
    target = approver_for(st, v)
    draft = templates.confirm(detect_lang(msg), name=v["client"], issue=a.summary, module=a.module,
                              outcome=a.desired_outcome, questions=a.open_questions, n=v["id"]) \
        if v["is_test"] and target else None
    analyst = (f"[Analyst] Request type: {a.ticket_type} ({a.nature}), track {a.track}. Module: {module}. "
               f"Language: {lang}. Urgency: {a.urgency}. Open questions: {len(a.open_questions)}.")
    if v["is_test"]:
        analyst += f" Understanding: {a.summary}"
    nxt = ("the reply draft and its approval card follow" if draft else
           "the reply is approved in the hub's Telegram flow (client text is not shown in Teamhub)"
           if not st.admin_link_template else "the reply is approved on the hub admin page (client text is not shown in Teamhub)")
    functional = (f"[Functional] Likely cause: {a.root_cause}. Scope: {a.scope}. Proposed answer: send the client a "
                  f"short message confirming what we understood; {nxt}.")
    return {"root": root, "analyst": analyst, "functional": functional, "draft": draft, "target": target,
            "coordinator": _coordinator(st, v, a, draft, target)}


def _coordinator(st: Settings, v: dict, a, draft: str | None, target: str) -> str:
    if v["owner"]:
        who = f"Owner: {v['owner'][1] or 'set'} (set in Odoo)."
    else:
        pick = RuleAssigner(default_team()).assign(a, {})
        who = f"Suggested owner: {pick['owner'].name} because {'; '.join(pick['factors'])}. Nothing is assigned in Odoo."
    if draft:
        step = f"Next step: approval of the reply draft by {st.approvers.get(target, 'the approver')}."
    elif st.admin_link_template and not v["is_test"]:
        step = (f"Next step: the reply is reviewed and approved on the hub admin page ({st.admin_link_template.format(tid=v['id'])}). "
                "Nothing is sent from Teamhub.")
    else:
        step = "Next step: handled in the hub's Telegram flow; no approval card here."
    return f"[Coordinator] {who} {step}"


# ------------------------------------------------------------------------------------------------ open a thread

def _fail(ctx: Ctx, tid: int, reason: str) -> None:
    """A permanent failure for this ticket: recorded, alerted, skipped on later runs until an operator clears it."""
    ctx.threads.patch(tid, failed=reason)
    ctx.out(f"ALERT: ticket {tid} marked failed: {reason}")


def _need_id(op: dict) -> str:
    return {"create_thread": "root_id", "reply": "message_id", "card": "message_id"}.get(op["op"], "")


def _outcome(ctx: Ctx, tid: int, op: dict, r: dict) -> str:
    """ok | refused | failed (permanent) | retry (transient: the poster was unavailable or said ok:false)."""
    if r.get("refused"):
        ctx.threads.patch(tid, refused=f"{op['key']}: {r['refused']}")
        ctx.out(f"ticket {tid}: {op['key']} REFUSED by the privacy guard ({r['refused']})")
        return "refused"
    if r.get("unknown") or r.get("capped"):
        _fail(ctx, tid, f"{op['key']}: {'unknown outcome' if r.get('unknown') else r.get('error')}")
        return "failed"
    if r.get("ok"):
        if _need_id(op) and not r.get(_need_id(op)):
            _fail(ctx, tid, f"{op['key']}: the poster's result has no {_need_id(op)}")
            return "failed"
        return "ok"
    ctx.out(f"ticket {tid}: {op['key']} not posted ({r.get('error') or 'poster failed'})")
    return "retry"


def _open_root(ctx: Ctx, v: dict, steps: dict, q) -> str:
    """Idempotent: the create key is t<id>-root and the root id is stored."""
    tid = v["id"]
    if ctx.threads.ensure(tid)["root_id"]:
        return "ok"
    op = create_op(tid, steps["root"])
    r = q.run([op])[0]
    state = _outcome(ctx, tid, op, r)
    if state == "ok":
        ctx.threads.patch(tid, root_id=r["root_id"], root_text=steps["root"])
        ctx.threads.mark(tid, "steps", "root")
    return state


def _step_ops(ctx: Ctx, v: dict, steps: dict, entry: dict) -> list[dict]:
    tid, root, done = v["id"], entry["root_id"], entry["steps"]
    ment = [steps["target"]] if steps["draft"] else None
    ops = [reply_op(tid, "analyst", root, steps["analyst"]), reply_op(tid, "functional", root, steps["functional"]),
           reply_op(tid, "coordinator", root, steps["coordinator"], ment)]
    if steps["draft"]:
        if "card" not in done:                            # the exact draft goes out just BEFORE its card (no client text in the card)
            ops.append(draft_op(tid, root, steps["draft"], f"t{tid}-draft"))
        doc = ctx.approvals.get(entry["card"].get("nonce") or "")
        stale = doc is not None and not doc.get("card_message_id") and \
            datetime.fromisoformat(doc["expires_at"]) <= ctx.approvals.clock()
        if doc is None or stale:                          # issued ONCE; an expired, never-posted card gets a new nonce
            doc = ctx.approvals.issue(tid, steps["draft"], steps["target"])
            ctx.threads.patch(tid, card={"nonce": doc["nonce"]})
        ops.append(card_op(tid, root, doc, ctx.st.approvers.get(steps["target"], "the approver")))
    return [o for o in ops if o["key"].split("-", 1)[1] not in done]


def open_thread(ctx: Ctx, v: dict) -> str:
    """Open (or resume) the ONE thread for this ticket. Returns opened | resumed | refused | failed | retry."""
    steps = build_steps(ctx.st, v, ticket_link(ctx, v["id"]))
    entry = ctx.threads.ensure(v["id"])
    if entry.get("failed"):
        return "failed"                                   # waiting for an operator
    if entry["refused"]:
        return "refused"
    route = route_for(ctx.st, v["owner"][0] if v["owner"] else 0)
    if route and not entry.get("route") and not entry["root_id"]:
        entry = ctx.threads.patch(v["id"], route=route)   # this ticket's own room, fixed when its thread opens
    q = queue_for(ctx, entry.get("route"))
    had_root = bool(entry["root_id"])
    state = _open_root(ctx, v, steps, q)
    if state != "ok":
        return state
    entry = ctx.threads.get(v["id"])
    ops = _step_ops(ctx, v, steps, entry)
    results = q.run(ops) if ops else []
    for op, r in zip(ops, results):
        state = _outcome(ctx, v["id"], op, r)
        if state != "ok":
            return state
        ctx.threads.mark(v["id"], "steps", op["key"].split("-", 1)[1])
        if op["op"] == "card":
            card = ctx.threads.get(v["id"])["card"]
            ctx.approvals.attach_card(card["nonce"], r["message_id"])
            ctx.threads.patch(v["id"], card={**card, "message_id": r["message_id"]})
    return "resumed" if had_root else "opened"


# ------------------------------------------------------------------------------------------------ close

def close_text(ctx: Ctx, tid: int, stage: str) -> str:
    doc = ctx.approvals.for_ticket(tid)
    who = ctx.st.approvers.get((doc or {}).get("used_by") or "", "an approver")
    how = {"approved": f"reply draft approved by {who}", "edited": f"reply draft edited and approved by {who}",
           "rejected": f"reply draft rejected by {who}"}.get((doc or {}).get("decision") or "", "no reply draft was decided")
    return f"[Closed] Outcome: ticket closed in Odoo (stage {stage or 'closed'}); {how}. Nothing was sent to the client."


def close_thread(ctx: Ctx, entry: dict, stage: str) -> str:
    """The close sequence, once: [Closed] reply, root marked [CLOSED], thread muted. ``closed`` only when all three
    worked, so a failed run resumes (each key is idempotent). Returns closed | failed | retry."""
    tid, root = entry["ticket_id"], entry["root_id"]
    text = close_text(ctx, tid, stage)                   # read the decision BEFORE the open card is superseded
    ctx.approvals.supersede(tid)
    marked = entry["root_text"] if entry["root_text"].startswith("[CLOSED]") else f"[CLOSED] {entry['root_text']}"
    ops = [reply_op(tid, "closed", root, text),
           {"key": f"t{tid}-closed-root", "op": "edit_root", "message": root, "text": marked},
           {"key": f"t{tid}-closed-mute", "op": "mute", "root": root}]
    states = []
    for op, r in zip(ops, queue_for(ctx, entry.get("route")).run(ops)):
        states.append(_outcome(ctx, tid, op, r))
        if states[-1] == "ok":
            ctx.threads.mark(tid, "closed_steps", op["key"].split("-", 1)[1])
    if all(x == "ok" for x in states):
        ctx.threads.patch(tid, closed=True)
        return "closed"
    return "failed" if "failed" in states else "retry"


# ------------------------------------------------------------------------------------------------ the run

def _new_tickets(ctx: Ctx, domain: list, closed: dict, summary: dict) -> None:
    mark = ctx.store.get("teamhub_state", "watch")
    if mark is None and ctx.st.seed_watermark and not ctx.dry_run:       # production: never open threads for the backlog
        top = ctx.odoo.search_read("helpdesk.ticket", domain, ["id"], limit=1, order="id desc")
        ctx.store.put("teamhub_state", "watch", {"last_id": int(top[0]["id"]) if top else 0})
        return
    mark = mark or {"last_id": 0}
    rows = ctx.odoo.search_read("helpdesk.ticket", domain + [["id", ">", int(mark["last_id"])]], FIELDS,
                                limit=ctx.st.batch, order="id asc")
    last, hold = int(mark["last_id"]), None
    for v in ticket_views(ctx.odoo, rows, ctx.st):
        if v["stage_id"] in closed and not ctx.threads.get(v["id"]):
            summary["skipped"].append(v["id"])           # already closed when first seen: no thread
        else:
            status = open_thread(ctx, v)
            if status == "retry" and hold is None:
                hold = v["id"]                            # the watermark stops before the first transient failure only
            summary[{"refused": "refused", "failed": "failed", "retry": "retry"}.get(status, "opened")].append(v["id"])
        last = v["id"]                                    # a failed ticket is skipped (recorded), so others go on
    ctx.store.put("teamhub_state", "watch", {"last_id": max(int(mark["last_id"]), min(last, (hold or last + 1) - 1))})


def _close_tracked(ctx: Ctx, closed: dict, summary: dict) -> None:
    tracked = {e["ticket_id"]: e for e in ctx.threads.open_threads()}
    if not tracked:
        return
    for r in ctx.odoo.search_read("helpdesk.ticket", [["id", "in", sorted(tracked)]], ["stage_id"]):
        sid = (_m2o(r.get("stage_id")) or (0, ""))[0]
        if sid in closed:
            res = close_thread(ctx, ctx.threads.get(int(r["id"])), closed[sid])
            summary["closed" if res == "closed" else res].append(int(r["id"]))


def watch_once(ctx: Ctx) -> dict:
    """One pass: new tickets get a thread; tracked tickets that closed get the close sequence."""
    domain = domain_from(ctx.st.filter)                  # refuses first, before any read
    closed = closed_stages(ctx.odoo)
    summary: dict = {"opened": [], "refused": [], "skipped": [], "closed": [], "failed": [], "retry": [],
                     "dry_run": ctx.dry_run}
    _new_tickets(ctx, domain, closed, summary)
    _close_tracked(ctx, closed, summary)
    return summary
