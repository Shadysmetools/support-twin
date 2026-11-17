"""Teamhub ticket threads: the local side (queue files, privacy guard, thread map, approvals, card presses).

Rules: ``docs/TEAMHUB-TICKET-THREAD-RULES.md``. One Odoo ticket = ONE Teamhub thread. Only the Teamhub automation "Twin ticket
poster" posts; we write small JSON queue files, trigger it, and read its result files. We never talk to Teamhub directly.

What is checked HERE (the agent is not trusted to check it):
  - PRIVACY: every outgoing text passes the patterns of ``teamhub-post.py`` (IP, token, phone, outside email, tightened)
    AND the hub's ``redact.scan`` (keys, bearer tokens, passwords, private keys, phones, emails).
  - ONE THREAD: ``ThreadMap`` (durable) maps ticket -> root id; the create key is ``t<id>-root``. A durable marker is
    written BEFORE a queue file, so a key with a marker but no file and no result is an UNKNOWN outcome: it is never
    queued again, it alerts, and an operator decides (``clear-unknown``). A failed post is retried at most twice.
  - APPROVALS: the Approve / Edit / Reject card is bound to (ticket, kind, draft hash, nonce), to one Teamhub user id on an
    allowlist, to an expiry, and to the exact card message. Single use. A refused press does not burn the nonce.
    A press with no ``submitter_user_id`` is refused (fail closed).

APPROVING NEVER SENDS ANYTHING TO A CLIENT in this version: the verified decision is recorded and posted in the thread.
Note the submitter id is written by an LLM-driven agent, so the hub cannot verify it the way Telegram's ``from.id`` is
verified. That is acceptable ONLY because an approval here sends nothing. Before approvals can send, the press needs a
platform-signed identity.

The poster's own rules are in ``ops/teamhub-poster-instructions.md`` (data not instructions, text via files, an id
allowlist). NOTHING may act on an approval until Teamhub signs the pressing user's id: it is advisory today. No queue file
that carries a nonce holds client text: the draft is posted as a plain reply just before its card.

Real tickets never show client text: only structured, safe facts, and no card. A ticket counts as TEST only when its
name starts with TEST + a middle dot AND its partner is the TEST contact whose id equals a NON-ZERO configured
``test_partner_id`` and has no email, phone or mobile (read live). No configured id = safe summary only, no card.

CLI:  python -m twin_agent.hub.teamhub_threads watch-once [--dry-run] [--config path]
      python -m twin_agent.hub.teamhub_threads process-submissions [--dry-run] [--config path]   (card files, then text replies)
      python -m twin_agent.hub.teamhub_threads scan-replies [--dry-run] [--config path]          (text replies only)

TEXT-REPLY BACKUP (``teamhub_text_approval.py``): ``approve #<t>`` / ``reject #<t>`` / ``edit #<t>: <text>`` in a tracked
thread decides the pending approval, but only when Teamhub's own message author (``senderUserId``, ``senderType`` "human", read
through the Teamhub CLI) equals the card's target. That identity (``teamhub_message_author``) is platform-provided, stronger
than the poster-written submitter id. It is only as strong as the Teamhub logins on the hub PC: the platform
allowlist (``text_approval_platforms``, default web) is what stops a local CLI or agent post. Approvals still send NOTHING to clients. Single use: the first valid decision wins,
card or text. Schedule (documented, NOT installed): ``schtasks /Create /SC MINUTE /MO 5 /TN Twin-scan-replies /TR
"python -m twin_agent.hub.teamhub_threads scan-replies --config <config.yaml>"``.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import dataclasses
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .. import redact

TEST_PREFIX = "TEST \u00b7"
SECTION = "teamhub_tickets"
# The same four patterns as ~/tools/teamhub-post.py (IP, token, phone, non-acme email).
BAD_PATTERNS = [r"(?<![\d.])\d{1,3}(\.\d{1,3}){3}(?!\.?\d)", r"(sk|ghp|gho|xox[bp]|AKIA|EAA)[A-Za-z0-9_\-]{10,}",
                r"(?i)\b(?:pw|pwd|pass|passwd|password)\s*[:=]\s*\S+",
                r"(?:كلمة السر|كلمة المرور|الباسورد)\s*(?:[:=]|هي|is)\s*\S+",
                r"[\w.+-]+\s*(?:\(at\)|\[at\]|\bat\b)\s*[\w-]+\s*(?:\(dot\)|\[dot\]|\bdot\b)\s*\w+",
                r"\+?(?:\d[\s()./-]?){9,}\d", r"[\w.+-]+@(?!example\.com(?![\w-]|\.\w))[\w-]+\.[\w.]+"]
MAX_TEXT = 3500
RETRY_CAP = 2          # a failed post is retried at most twice, then the ticket is marked failed
ACTIONS = ("approve", "edit", "reject", "approve_edited")
DECISION = {"approve": "approved", "approve_edited": "edited", "edit": "edit_requested", "reject": "rejected"}
FINAL = ("approved", "edited", "rejected")
WHY = {"not_authorised": "that person is not allowed to decide this ticket",
       "used": "this approval was already used", "expired": "this approval expired; a new card is needed",
       "draft_mismatch": "the stored draft no longer matches its hash",
       "wrong_card": "the press did not come from the card issued for this approval",
       "no_submitter": "Teamhub did not say who pressed the button, so it cannot be verified",
       "text_changed_use_edit": "the text was changed; use Edit to approve changed text",
       "empty_text": "the edited text is empty", "placeholder_left": "an unfilled [[placeholder]] remains",
       "edit_sensitive": "the edited text looks like it holds private data", "unknown_action": "unknown button",
       "unknown_approval": "no such approval", "ticket_decided": "this ticket already has a final decision", "revoked": "this card was replaced by a newer one: use the newest card", "card_not_recorded": "the card for this approval is not recorded"}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_test_name(name: str) -> bool:
    return str(name or "").startswith(TEST_PREFIX)


# ------------------------------------------------------------------------------------------------ config

class ConfigError(ValueError):
    """A required setting is missing. The message names the key, never a value."""


@dataclass
class Settings:
    room_id: str
    workflow_id: str
    teamhub_exe: str
    workdir: str
    state_dir: str
    queue_dir: str = ""
    results_dir: str = ""
    wait_seconds: float = 90.0
    poll_seconds: float = 1.0
    trigger_retries: int = 2
    approval_ttl_hours: float = 24.0
    approvers: dict = field(default_factory=dict)          # teamhub_user_id -> display name
    approver_allowlist: list = field(default_factory=list)  # teamhub user ids who may press any card
    default_approver: str = ""
    owner_teamhub_ids: dict = field(default_factory=dict)     # odoo user id (str) -> teamhub user id
    odoo_secret_file: str = ""
    odoo_link_template: str = ""                            # e.g. https://host/web#id={tid}&model=helpdesk.ticket&view_type=form
    filter: dict = field(default_factory=dict)              # {"team_id": 2, "name_like": "TEST \u00b7%"}
    batch: int = 50
    owner_rooms: dict = field(default_factory=dict)         # odoo uid -> {room_id, workflow_id, approver[, queue_dir, results_dir]}
    gateway_check: bool = True                              # one `teamhub gateway status` check before posting
    test_partner_id: int = 0                                # the TEST contact id the tool created (0 = match by name)
    close_stage_id: int = 0                                 # the stage teamhub_test_ticket close writes (e.g. 4 Cancelled)
    text_approval_platforms: list = field(default_factory=lambda: ["web"])   # senderPlatform values a text approval may come from
    admin_link_template: str = ""                           # e.g. https://admin.example.com/tickets/{tid}: where a REAL ticket's reply is approved
    seed_watermark: bool = False                            # first run: start after the newest existing ticket (no backlog threads)
    trust_scoped_card_target: bool = False                  # a press with no user id = the card's single target

    @classmethod
    def from_cfg(cls, cfg: dict) -> "Settings":
        c = dict((cfg or {}).get(SECTION) or {})
        for k in ("room_id", "workflow_id", "teamhub_exe", "workdir", "state_dir"):
            if not c.get(k):
                raise ConfigError(f"{SECTION}.{k} is not set")
        known = {f for f in cls.__dataclass_fields__}
        st = cls(**{k: v for k, v in c.items() if k in known})
        w = Path(st.workdir)
        st.queue_dir = st.queue_dir or str(w / "relay-queue-tickets")
        st.results_dir = st.results_dir or str(w / "out" / "tickets")
        st.approvers = {str(k): str(v) for k, v in (st.approvers or {}).items()}
        st.approver_allowlist = [str(x) for x in (st.approver_allowlist or list(st.approvers))]
        st.owner_rooms = {str(k): dict(v) for k, v in (st.owner_rooms or {}).items()}
        st.owner_teamhub_ids = {str(k): str(v) for k, v in (st.owner_teamhub_ids or {}).items()}
        return st


def load_settings(path: str) -> Settings:
    import yaml
    with open(path, encoding="utf-8") as f:
        return Settings.from_cfg(yaml.safe_load(f) or {})


# ------------------------------------------------------------------------------------------------ privacy guard

class Refused(Exception):
    """An outgoing post was refused by the privacy guard. The message says why, never the text."""


def _strings(node: Any):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _strings(v)


def refusal_reason(op: dict) -> str | None:
    """None when the post may go; else a plain reason. Scans only what gets POSTED: text, fallback text, and the
    card's display blocks (never the actions, so a button value such as a nonce cannot trip the token pattern)."""
    texts = [op.get("text"), op.get("fallback_text")] + list(_strings(op.get("blocks")))
    texts = [t for t in texts if isinstance(t, str)]
    if op.get("op") in ("create_thread", "reply", "edit_root") and not str(op.get("text") or "").strip():
        return "empty text"
    if op.get("op") == "card" and not str(op.get("fallback_text") or "").strip():
        return "empty card text"
    for t in texts:
        if len(t) > MAX_TEXT:
            return f"longer than {MAX_TEXT} characters"
        if any(re.search(b, t) for b in BAD_PATTERNS):
            return "looks like it contains an IP / token / phone / outside email"
        kinds = sorted({f.kind for f in redact.scan(t) if not _team_email(t, f)})
        if kinds:
            return f"the scanner found private data ({', '.join(kinds)})"
    return None


def _team_email(t: str, f) -> bool:
    return f.kind == "email" and t[f.start:f.end].lower().endswith("@example.com")


def stage_flag(fields: dict) -> str:
    """The ONE rule for 'closed stage' (watcher and test tool): ``fold`` first, else ``is_close``, else refuse."""
    flag = "fold" if "fold" in fields else ("is_close" if "is_close" in fields else "")
    if not flag:
        raise ConfigError("cannot tell which helpdesk stages are closed (no fold / is_close field)")
    return flag


@contextlib.contextmanager
def single_instance(state_dir: str):
    """One watcher/processor at a time (an OS file lock in the state folder)."""
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    f = open(Path(state_dir) / "teamhub.lock", "a+")
    try:
        f.write("x")
        f.flush()
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise Refused("another Teamhub watcher or press processor is already running") from None
    try:
        yield
    finally:
        f.close()


# ------------------------------------------------------------------------------------------------ stores

class OverlayStore:
    """For ``--dry-run``: reads see the real store, writes stay in memory and vanish. Nothing is persisted."""

    def __init__(self, base=None):
        from .store import MemoryStore
        self.base, self.mem = base, MemoryStore()

    def get(self, ns, key):
        d = self.mem.get(ns, key)
        return d if d is not None else (self.base.get(ns, key) if self.base else None)

    def put(self, ns, key, doc):
        self.mem.put(ns, key, doc)

    def put_if_absent(self, ns, key, doc):
        if self.get(ns, key) is not None:
            return False
        self.mem.put(ns, key, doc)
        return True

    def update(self, ns, key, fn):
        cur = self.get(ns, key)
        if cur is None:
            return None
        new = fn(json.loads(json.dumps(cur)))
        self.mem.put(ns, key, new)
        return new

    def scan(self, ns):
        ident = lambda d: (d.get("ticket_id"), d.get("nonce"))      # noqa: E731 - threads and approvals carry these
        out = list(self.mem.scan(ns))
        mine = {ident(d) for d in out}
        return out + [d for d in (self.base.scan(ns) if self.base else []) if ident(d) not in mine]


class ThreadMap:
    """Durable ticket -> thread record. One thread per ticket, across re-runs and crashes."""

    NS = "teamhub_threads"

    def __init__(self, store):
        self.store = store

    def get(self, tid: int) -> dict | None:
        return self.store.get(self.NS, str(int(tid)))

    def ensure(self, tid: int) -> dict:
        self.store.put_if_absent(self.NS, str(int(tid)), {
            "ticket_id": int(tid), "root_id": "", "root_text": "", "steps": [], "card": {}, "closed": False,
            "closed_steps": [], "refused": "", "decision": None, "failed": "", "route": None})
        return self.get(tid)

    def patch(self, tid: int, **kw) -> dict:
        return self.store.update(self.NS, str(int(tid)), lambda d: {**d, **kw})

    def mark(self, tid: int, field_: str, step: str) -> None:
        def fn(d: dict) -> dict:
            if step not in d[field_]:
                d[field_] = d[field_] + [step]
            return d
        self.store.update(self.NS, str(int(tid)), fn)

    def open_threads(self) -> list[dict]:
        return [d for d in self.store.scan(self.NS) if d.get("root_id") and not d.get("closed") and not d.get("failed")]


# ------------------------------------------------------------------------------------------------ the queue

class TeamhubQueue:
    """Writes queue files for the poster, triggers it, waits for result files. ``dry_run`` writes nothing and only
    prints what WOULD be queued. ``runner`` / ``sleep`` / ``clock`` / ``ns`` are injectable so tests are fast.

    SAFETY: a durable "queued key K at T" marker is written to the store BEFORE the queue file. A key with a marker,
    no queue file and no result is an UNKNOWN outcome (the poster may have posted): it is never queued again, it is
    reported (ALERT) and the ticket is marked failed until an operator decides. ``ok:false`` means NOTHING was
    posted; such a post is retried at most ``RETRY_CAP`` times (``<key>~1``, ``~2``)."""

    def __init__(self, st: Settings, *, dry_run: bool = False, runner: Callable = subprocess.run,
                 sleep: Callable = time.sleep, clock: Callable = time.monotonic, ns: Callable = time.time_ns,
                 out: Callable = print, store: Any = None, now: Callable = None):
        self.st, self.dry_run, self.runner, self.sleep, self.clock, self.ns, self.out = st, dry_run, runner, sleep, clock, ns, out
        self.store, self.now, self._last, self.down = store, now or utcnow, 0, False

    # -- gateway
    def check_gateway(self) -> None:
        """One up-front check. A dead gateway refuses the WHOLE run (no waiting per item)."""
        if self.dry_run or not self.st.gateway_check:
            return
        try:
            r = self.runner([self.st.teamhub_exe, "gateway", "status"], cwd=os.path.expanduser("~"),
                            capture_output=True, text=True, timeout=30)
            ok = getattr(r, "returncode", 1) == 0 and "running and connected" in (getattr(r, "stdout", "") or "").lower()
        except Exception:                                 # noqa: BLE001
            ok = False
        if not ok:
            self.down = True
            raise Refused("the Teamhub gateway is down on this computer: nothing was posted. Start it, then run again.")

    # -- files
    def result(self, key: str) -> dict | None:
        p = Path(self.st.results_dir) / f"{key}.result.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None                                   # absent or half-written: not ready yet
        return d if isinstance(d, dict) and "ok" in d else None

    def _pending(self, key: str) -> bool:
        q = Path(self.st.queue_dir)
        return q.is_dir() and any(q.glob(f"*-{key}.json"))

    def _write(self, op: dict) -> None:
        q = Path(self.st.queue_dir)
        q.mkdir(parents=True, exist_ok=True)
        self._last = max(self.ns(), self._last + 1)       # strictly increasing: oldest filename = first posted
        final = q / f"{self._last:020d}-{op['key']}.json"
        tmp = q / f".{final.name}.tmp"
        tmp.write_text(json.dumps(op, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, final)                            # atomic: the poster never sees a half file

    def _marked(self, key: str) -> bool:
        m = self.store.get("teamhub_queue", key) if self.store is not None else None
        return bool(m) and not m.get("cleared")

    def clear_marker(self, key: str) -> bool:
        """Operator: the key was NOT posted (or is settled): allow it to be queued again."""
        if self.store is None or self.store.get("teamhub_queue", key) is None:
            return False
        self.store.update("teamhub_queue", key, lambda d: {**d, "cleared": True})
        return True

    def _resolve(self, base: str) -> tuple[str, dict | None]:
        """The key to use: a finished result is reused; a FAILED attempt moves on to ``<key>~1``, ``~2``; after
        ``RETRY_CAP`` retries the post is given up (``capped``)."""
        for n in range(RETRY_CAP + 1):
            key = base if n == 0 else f"{base}~{n}"
            res = self.result(key)
            if res is None or res.get("ok"):
                return key, res
        return base, {"ok": False, "capped": True, "key": base, "error": f"failed {RETRY_CAP + 1} times; not retried"}

    # -- run
    def run(self, ops: list[dict]) -> list[dict]:
        """Queue every op not yet done, trigger the poster once, wait. One result dict per op, in order."""
        results: list[dict | None] = [None] * len(ops)
        waiting: list[tuple[int, str]] = []
        for i, op in enumerate(ops):
            why = refusal_reason(op)
            if why:
                results[i] = {"ok": False, "refused": why, "key": op["key"]}
            elif self.dry_run:
                self.out("WOULD QUEUE " + json.dumps({k: v for k, v in op.items() if k != "blocks"}, ensure_ascii=False))
                results[i] = {"ok": True, "dry_run": True, "key": op["key"], "root_id": "DRYRUN-ROOT",
                              "message_id": "DRYRUN-MSG"}
            else:
                self._plan(op, i, results, waiting)
        if waiting:
            self._trigger_and_wait(waiting, results)
        return results  # type: ignore[return-value]

    def _plan(self, op: dict, i: int, results: list, waiting: list) -> None:
        key, res = self._resolve(op["key"])
        if res is not None:
            results[i] = res
        elif self._pending(key):                          # still in the queue: just trigger and wait
            waiting.append((i, key))
        elif self._marked(key):                           # taken by the poster, no result: it MAY have posted
            self.out(f"ALERT: {key} was queued before but has no result: outcome UNKNOWN, NOT posting it again")
            results[i] = {"ok": False, "unknown": True, "key": key, "error": "unknown outcome"}
        elif self.down:
            results[i] = {"ok": False, "error": "poster unavailable", "key": key}
        else:
            if self.store is not None:                    # write-ahead: durable BEFORE the file exists
                self.store.put("teamhub_queue", key, {"key": key, "queued_at": self.now().isoformat()})
            self._write({**op, "key": key})
            waiting.append((i, key))

    def _trigger(self) -> None:
        cmd = [self.st.teamhub_exe, "workspace", "workflow", "trigger", self.st.workflow_id, "--scope-room", self.st.room_id]
        try:
            self.runner(cmd, cwd=os.path.expanduser("~"), capture_output=True, text=True, timeout=60)
        except Exception as e:                            # noqa: BLE001 - a failed trigger only means "wait / retry"
            self.out(f"trigger failed: {type(e).__name__}")

    def _wait(self, keys: list[str]) -> bool:
        deadline = self.clock() + self.st.wait_seconds
        while True:
            if all(self.result(k) is not None for k in keys):
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(self.st.poll_seconds)

    def _trigger_and_wait(self, waiting: list[tuple[int, str]], results: list) -> None:
        keys = [k for _, k in waiting]
        done = False
        for _ in range(1 + self.st.trigger_retries):      # first trigger + at most two retries
            self._trigger()
            if self._wait(keys):
                done = True
                break
        if not done:
            self.down = True                              # the rest of this run fails fast
        for i, key in waiting:
            results[i] = self.result(key) or {"ok": False, "error": "poster unavailable", "key": key}


# ------------------------------------------------------------------------------------------------ approvals

def _verdict(doc: dict, who: str, action: str, text: str | None, source_mid: str, now: datetime) -> str | None:
    """None = accept; else the reason code. Pure, so a dry run can ask without burning anything."""
    if who not in doc["allowed"]:
        return "not_authorised"
    if doc.get("used_at") == "revoked":
        return "revoked"
    if doc.get("used_at"):
        return "used"
    if datetime.fromisoformat(doc["expires_at"]) <= now:
        return "expired"
    if sha(doc["draft"]) != doc["draft_hash"]:
        return "draft_mismatch"
    if not doc.get("card_message_id"):
        return "card_not_recorded"
    if source_mid != doc["card_message_id"]:
        return "wrong_card"
    if (action == "approve_edited") != (doc.get("kind", "reply") == "edit"):
        return "unknown_action"                           # the edit card takes only "approve edited text"
    if action == "approve" and text is not None and sha(text) != doc["draft_hash"]:
        return "text_changed_use_edit"
    if action == "approve_edited":
        return _edit_problem(doc, text)
    return None


def _edit_problem(doc: dict, text: str | None) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return "empty_text"
    if "[[" in text:
        return "placeholder_left"
    fresh = [f for f in redact.scan(text) if text[f.start:f.end] not in doc["draft"]]
    if fresh or refusal_reason({"op": "reply", "text": text}):
        return "edit_sensitive"
    return None


class ApprovalBook:
    """Bound, single-use approvals for the reply draft. Same semantics as the hub's ``owner_flow`` approvals, but the
    approver is a Teamhub user id (a string) and a decision NEVER sends: it is recorded and posted in the thread."""

    NS = "teamhub_approvals"

    def __init__(self, store, st: Settings, clock: Callable = utcnow):
        self.store, self.st, self.clock = store, st, clock

    def issue(self, ticket_id: int, draft: str, target: str, *, kind: str = "reply", parent: str = "",
              only_target: bool = False) -> dict:
        now = self.clock()
        allowed = [target] if kind == "edit" or only_target else sorted({target, *self.st.approver_allowlist})
        nonce = secrets.token_hex(16)
        doc = {"nonce": nonce, "ticket_id": int(ticket_id), "kind": kind, "parent": parent, "draft": draft, "draft_hash": sha(draft),
               "target": target, "allowed": allowed, "issued_at": now.isoformat(),
               "expires_at": (now + timedelta(hours=self.st.approval_ttl_hours)).isoformat(), "used_at": None,
               "used_by": None, "decision": None, "approved_text": None, "approved_hash": None, "card_message_id": None}
        self.store.put(self.NS, nonce, doc)
        return doc

    def get(self, nonce: str) -> dict | None:
        return self.store.get(self.NS, str(nonce))

    def attach_card(self, nonce: str, message_id: str) -> None:
        self.store.update(self.NS, nonce, lambda d: {**d, "card_message_id": str(message_id)})

    def check(self, nonce: str, who: str, action: str, text: str | None, source_mid: str) -> str | None:
        doc = self.get(nonce)
        return "unknown_approval" if doc is None else _verdict(doc, who, action, text, source_mid, self.clock())

    def decide(self, nonce: str, who: str, action: str, text: str | None, source_mid: str,
               identity: str = "explicit", press_id: str = "") -> tuple[dict | None, str | None]:
        """Atomically re-check and consume. A refusal leaves the nonce live."""
        now, out = self.clock(), {}

        def fn(doc: dict) -> dict:
            out["reason"] = _verdict(doc, who, action, text, source_mid, now)
            if out["reason"]:
                return doc
            final = text if action == "approve_edited" else (doc["draft"] if action == "approve" else None)
            doc.update(used_at=now.isoformat(), used_by=who, identity=identity, press_id=press_id,
                       decision=DECISION[action], approved_text=final,
                       approved_hash=sha(final) if final is not None else None)
            return doc

        doc = self.store.update(self.NS, nonce, fn)
        return (doc, "unknown_approval") if doc is None else (doc, out["reason"])

    def supersede(self, ticket_id: int, mark: str = "superseded") -> None:
        """The ticket closed (or the card was re-posted): any card still open can no longer be pressed."""
        for d in self.store.scan(self.NS):
            if d["ticket_id"] == int(ticket_id) and not d.get("used_at"):
                self.store.update(self.NS, d["nonce"], lambda x: {**x, "used_at": mark})

    def revoke(self, ticket_id: int) -> None:
        self.supersede(ticket_id, "revoked")

    def issue_edit(self, parent: dict, who: str) -> dict:
        """The second step of Edit: a NEW nonce for the approver who asked, prefilled with the draft."""
        existing = [d for d in self.store.scan(self.NS) if d.get("parent") == parent["nonce"]]
        return existing[0] if existing else self.issue(parent["ticket_id"], parent["draft"], who, kind="edit",
                                                       parent=parent["nonce"])

    def decided(self, ticket_id: int) -> bool:
        """True when ANY approval of the ticket (the card or its edit card) has a final decision."""
        return any(d["ticket_id"] == int(ticket_id) and d.get("decision") in FINAL for d in self.store.scan(self.NS))

    def for_ticket(self, ticket_id: int) -> dict | None:
        """The latest DECIDED approval for the ticket, else the latest one."""
        docs = [d for d in self.store.scan(self.NS) if d["ticket_id"] == int(ticket_id)]
        decided = [d for d in docs if d.get("decision") in FINAL]
        pool = decided or docs
        return max(pool, key=lambda d: d["issued_at"]) if pool else None


# ------------------------------------------------------------------------------------------------ cards and ops

def create_op(tid: int, text: str) -> dict:
    return {"key": f"t{tid}-root", "op": "create_thread", "text": text}


def reply_op(tid: int, step: str, root: str, text: str, mentions: list[str] | None = None) -> dict:
    op = {"key": f"t{tid}-{step}", "op": "reply", "root": root, "text": text}
    if mentions:
        op["mentions"] = list(mentions)
    return op


def _quote(text: str) -> str:
    return "\n".join(f"> {ln}" if ln.strip() else ">" for ln in text.splitlines())


def fingerprint(text: str) -> str:
    h = sha(text)
    return f"{h[:4]}-{h[4:8]}"


def draft_op(tid: int, root: str, draft: str, key: str, note: str = "") -> dict:
    """The exact draft, posted as a plain reply JUST BEFORE its card. Client text lives here, never in a queue file that
    carries a nonce (the poster is an LLM automation: a nonce next to client text is the pair to keep apart)."""
    text = (f"[Coordinator] Proposed reply for ticket #{tid} (exact text, fingerprint {fingerprint(draft)}). "
            f"Nothing is sent to the client in this version.{note}\n\n{draft}")
    return {"key": key, "op": "reply", "root": root, "text": text}


def card_op(tid: int, root: str, doc: dict, who: str, *, key: str | None = None, replaces: str | None = None) -> dict:
    """The Approve / Edit / Reject card for ONE approver. SIMPLE (no form: a form field can block the click) and it holds
    NO client text: the exact draft is in the reply posted just before it; the card carries its fingerprint."""
    exp, fp = doc["expires_at"], fingerprint(doc["draft"])
    fallback = (f"[Coordinator] Approval needed for ticket #{tid}, from {who} only. The exact reply text is in the message "
                f"just above (fingerprint {fp}). Nothing is sent to the client in this version. Expires {exp}.")
    blocks: list[dict] = [
        {"type": "markdown", "content": f"Approval for ticket #{tid}: read the proposed reply in the message just above "
                                        f"(fingerprint {fp}). Approving records the decision only: nothing is sent to "
                                        "the client."},
        {"type": "key_values", "items": [{"label": "Ticket", "value": f"#{tid}"}, {"label": "Approver", "value": who},
                                         {"label": "Fingerprint", "value": fp},
                                         {"label": "Expires", "value": exp, "format": "datetime"}]}]
    actions = [{"type": "button", "actionId": "approve", "label": "Approve", "style": "primary", "value": doc["nonce"]},
               {"type": "button", "actionId": "edit", "label": "Edit", "value": doc["nonce"]},
               {"type": "button", "actionId": "reject", "label": "Reject", "style": "danger", "value": doc["nonce"]}]
    op = {"key": key or f"t{tid}-card", "op": "card", "root": root, "fallback_text": fallback, "blocks": blocks,
          "actions": actions, "target_user": doc["target"]}
    if replaces:
        op["replaces"] = str(replaces)
    return op


def edit_card_op(tid: int, root: str, doc: dict) -> dict:
    """Step two of Edit: an EMPTY textarea (the current text is in the reply just before) and ONE button. The text that
    comes back is what is approved. No client text in this file either."""
    fallback = (f"[Coordinator] Edit the reply for ticket #{tid}: paste the edited text, then press Approve edited text. "
                f"The current text is in the message just above. Expires {doc['expires_at']}.")
    blocks = [{"type": "markdown", "content": f"Edit the reply for ticket #{tid}: the current text is in the message just "
                                              "above. Paste your edited text, then press Approve edited text. The text "
                                              "you submit is the text that is approved (nothing is sent to the client)."},
              {"type": "field_group", "fields": [{"type": "textarea", "fieldId": "text", "minRows": 4,
                                                  "placeholder": "Paste the edited reply here"}]},
              {"type": "key_values", "items": [{"label": "Expires", "value": doc["expires_at"], "format": "datetime"}]}]
    actions = [{"type": "button", "actionId": "approve_edited", "label": "Approve edited text", "style": "primary",
                "value": doc["nonce"]}]
    return {"key": f"t{tid}-editcard-{doc['nonce'][:8]}", "op": "card", "root": root, "fallback_text": fallback,
            "blocks": blocks, "actions": actions, "target_user": doc["target"]}


# ------------------------------------------------------------------------------------------------ routes (per-person rooms)

def route_for(st: Settings, owner_uid) -> dict | None:
    """Config only: an owner's own room/automation/approver (e.g. principals-tickets). None = the default room."""
    r = (st.owner_rooms or {}).get(str(owner_uid or ""))
    return dict(r) if r else None


def queue_for(ctx: "Ctx", route: dict | None) -> TeamhubQueue:
    if not route:
        return ctx.queue
    key = json.dumps(route, sort_keys=True)
    if key not in ctx._queues:
        q, st = copy.copy(ctx.queue), ctx.queue.st
        q.st = dataclasses.replace(st, room_id=route.get("room_id") or st.room_id,
                                   workflow_id=route.get("workflow_id") or st.workflow_id,
                                   queue_dir=route.get("queue_dir") or st.queue_dir,
                                   results_dir=route.get("results_dir") or st.results_dir)
        ctx._queues[key] = q
    return ctx._queues[key]


# ------------------------------------------------------------------------------------------------ card presses

def _submission_fields(sub: dict) -> tuple[str, str, str, str | None, str]:
    """(who, action, nonce, text, source_message_id) from a raw submission file. ``inputValues`` is accepted as an
    alias of ``submitted_values`` (the documented Teamhub name)."""
    vals = sub.get("submitted_values")
    if not isinstance(vals, dict):
        vals = sub.get("inputValues") if isinstance(sub.get("inputValues"), dict) else {}
    text = vals.get("text")
    who = sub.get("submitter_user_id")
    return (who.strip() if isinstance(who, str) else "", str(sub.get("action_id") or sub.get("actionId") or ""),
            str(sub.get("value") or ""), text if isinstance(text, str) else None,
            str(sub.get("source_message_id") or sub.get("sourceMessageId") or ""))


def _reply_text(st: Settings, doc: dict | None, action: str, who: str, reason: str | None) -> str:
    if reason:
        return f"[Coordinator] Press refused: {WHY.get(reason, reason)}. Nothing was approved and nothing was sent."
    name = st.approvers.get(who, "an approver")
    if action == "edit":
        return f"[Coordinator] Edit requested by {name}: a new card with the text follows. Nothing was sent to the client."
    if action == "reject":
        return f"[Coordinator] Rejected by {name}. Nothing was sent to the client."
    verb = "Approved" if action == "approve" else "Edited and approved"
    via = " Identity came from Teamhub's card scoping, not an explicit id." if doc.get("identity") == "teamhub_scoping" else ""
    return (f"[Coordinator] {verb} by {name} (text fingerprint {doc['approved_hash'][:4]}-{doc['approved_hash'][4:8]}). Recorded only: "
            f"nothing was sent to the client.{via}")


@dataclass
class Ctx:
    """Everything a run needs. Built once by the CLI; tests build it with fakes."""

    st: Settings
    store: Any
    queue: TeamhubQueue
    threads: ThreadMap
    approvals: ApprovalBook
    odoo: Any = None
    dry_run: bool = False
    out: Callable = print
    _queues: dict = field(default_factory=dict)
    cli_runner: Callable = subprocess.run                   # read-only ``teamhub workspace thread get`` (text replies)


def _move_processed(ctx: Ctx, path: Path) -> None:
    if ctx.dry_run:
        return
    dest = path.parent / "processed"
    dest.mkdir(exist_ok=True)
    os.replace(path, dest / path.name)


def _scoped_target(st: Settings, doc: dict) -> bool:
    """The stored card record shows exactly ONE target and that target is on the allowlist."""
    t = doc.get("target")
    return isinstance(t, str) and bool(t) and t in st.approver_allowlist


_ACTION_OF = {"approved": "approve", "edited": "approve_edited", "rejected": "reject", "edit_requested": "edit"}


def _decide_one(ctx: Ctx, sub: dict, stem: str) -> dict:
    who, action, nonce, text, mid = _submission_fields(sub)
    doc = ctx.approvals.get(nonce)
    if doc is None:
        return {"ticket_id": 0, "reply": "", "reason": "unknown_approval"}
    identity = "explicit"
    if not who and ctx.st.trust_scoped_card_target and _scoped_target(ctx.st, doc):
        who, identity = doc["target"], "teamhub_scoping"       # Teamhub itself only lets the one target press this card
    if not who:
        reason = "no_submitter"
    elif action not in ACTIONS:
        reason = "unknown_action"
    elif ctx.approvals.decided(doc["ticket_id"]) and doc.get("press_id") != stem and not doc.get("decision"):
        reason = "ticket_decided"                         # one final decision per ticket (a repost cannot add a second)
    elif ctx.dry_run:
        reason = ctx.approvals.check(nonce, who, action, text, mid)
    else:
        new, reason = ctx.approvals.decide(nonce, who, action, text, mid, identity, stem)
        doc = new or doc
        if reason == "used" and doc.get("press_id") == stem and doc.get("decision"):
            reason, action = None, _ACTION_OF[doc["decision"]]   # a crash after deciding: this SAME press, report success
    out = {"ticket_id": doc["ticket_id"], "reason": reason, "reply": _reply_text(ctx.st, doc, action, who, reason)}
    if not reason and not ctx.dry_run:
        if doc.get("decision") == "edit_requested":      # step two of Edit: a NEW card (new nonce) for the same approver
            out["edit_nonce"] = ctx.approvals.issue_edit(doc, who)["nonce"]
        else:
            ctx.threads.patch(doc["ticket_id"], decision={"by": who, "decision": doc["decision"], "nonce": nonce,
                                                          "identity": doc.get("identity", identity)})
    return out


def results_dirs(st: Settings) -> list[Path]:
    """The default results folder plus every distinct per-room results folder (presses in a routed room land there)."""
    dirs = [st.results_dir] + [r["results_dir"] for r in (st.owner_rooms or {}).values() if r.get("results_dir")]
    return [Path(d) for d in dict.fromkeys(dirs)]


def process_submissions(ctx: Ctx) -> list[dict]:
    """Each submission file is handled once: decide (or refuse), post the result in the thread, move to processed/."""
    outcomes = []
    for d in results_dirs(ctx.st):
        for path in sorted(d.glob("submission-*.json")) if d.is_dir() else []:
            try:
                sub = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                sub = None
            if not isinstance(sub, dict):
                ctx.out(f"unreadable submission {path.name}: skipped")
                _move_processed(ctx, path)
                continue
            prior = ctx.store.get("teamhub_submissions", path.stem)
            if prior is None:                              # decide ONCE; a retry only re-posts the stored reply
                prior = _decide_one(ctx, sub, path.stem)
                ctx.store.put_if_absent("teamhub_submissions", path.stem, prior)
            outcomes.append({"file": path.name, **{k: prior[k] for k in ("ticket_id", "reason")}})
            if _post_outcome(ctx, path, prior):
                _move_processed(ctx, path)
    return outcomes


def process_all(ctx: Ctx) -> list[dict]:
    """One run: card press files, then card presses read back from the card, then text replies (pending approvals only)."""
    return process_submissions(ctx) + scan_pending(ctx)


def scan_pending(ctx: Ctx) -> list[dict]:
    """Platform-recorded decisions for every thread with a pending approval: card presses first, then text replies."""
    from .teamhub_card_press import scan_card_presses
    from .teamhub_text_approval import scan_replies
    return scan_card_presses(ctx) + scan_replies(ctx)


def _post_outcome(ctx: Ctx, path: Path, prior: dict) -> bool:
    tid, entry = prior["ticket_id"], ctx.threads.get(prior["ticket_id"]) if prior["ticket_id"] else None
    if not entry or not entry.get("root_id") or not prior["reply"]:
        ctx.out(f"{path.name}: refused ({prior['reason']}); no thread to tell")
        return True
    ops = [reply_op(tid, f"press-{sha(path.stem)[:10]}", entry["root_id"], prior["reply"])]
    edoc = ctx.approvals.get(prior.get("edit_nonce") or "")
    if edoc:
        ops.append(draft_op(tid, entry["root_id"], edoc["draft"], f"t{tid}-editdraft-{edoc['nonce'][:8]}"))
        ops.append(edit_card_op(tid, entry["root_id"], edoc))
    res = queue_for(ctx, entry.get("route")).run(ops)
    if edoc and res[2].get("ok") and res[2].get("message_id"):
        ctx.approvals.attach_card(edoc["nonce"], res[2]["message_id"])
    return all(r.get("ok") or r.get("refused") or r.get("capped") for r in res)    # a capped post is given up, not retried forever


def repost_card(ctx: Ctx, tid: int) -> dict:
    """Re-post the approval card: REVOKE the old nonce (a later press on the old card is refused as revoked), mint a new
    nonce with a fresh expiry, queue the simple card with ``replaces`` = the old card's message id, update the map.
    Refused when the ticket already has a final decision (also an approved edit) or a previous repost is still pending."""
    entry = ctx.threads.get(tid)
    card = (entry or {}).get("card") or {}
    old = ctx.approvals.get(card.get("nonce") or "")
    if not entry or not entry.get("root_id") or old is None:
        raise Refused(f"ticket {tid} has no thread/card to re-post")
    if entry.get("closed") or ctx.approvals.decided(tid):
        raise Refused(f"ticket {tid}: the ticket already has a final decision or is closed")
    q = queue_for(ctx, entry.get("route"))
    pend = card.get("pending_key")
    if pend and q.result(pend) is None and (q._pending(pend) or q._marked(pend)):
        raise Refused(f"ticket {tid}: the previous re-post ({pend}) is still pending or unknown; wait or clear it first")
    n = int(card.get("repost_n", 0)) + 1
    replaces = card.get("message_id") or old.get("card_message_id") or card.get("replaces") or ""
    ctx.approvals.revoke(tid)
    new = ctx.approvals.issue(tid, old["draft"], old["target"])
    key = f"t{tid}-card-r{n}-{new['nonce'][:8]}"            # the nonce is in the key: a stale queued file can never be reused
    ctx.threads.patch(tid, card={**card, "nonce": new["nonce"], "message_id": "", "replaces": replaces,
                                 "repost_n": n, "pending_key": key})
    who = ctx.st.approvers.get(new["target"], "the approver")
    op = card_op(tid, entry["root_id"], new, who, key=key, replaces=replaces or None)
    r = q.run([draft_op(tid, entry["root_id"], new["draft"], f"t{tid}-draft-r{n}-{new['nonce'][:8]}"), op])[1]
    ok = bool(r.get("ok") and r.get("message_id"))
    if ok:
        ctx.approvals.attach_card(new["nonce"], r["message_id"])
        ctx.threads.patch(tid, card={"nonce": new["nonce"], "message_id": r["message_id"], "repost_n": n,
                                     "replaces": replaces})
    else:
        ctx.out(f"ALERT: the new card for ticket {tid} was not posted ({r.get('error') or r.get('refused')}); the old card is revoked")
    return {"ticket_id": tid, "reposted": ok, "key": key, "replaces": replaces, "dry_run": ctx.dry_run}


# ------------------------------------------------------------------------------------------------ CLI

def build_ctx(st: Settings, *, dry_run: bool, need_odoo: bool) -> Ctx:
    from .store import SqliteStore
    db = Path(st.state_dir) / "teamhub.sqlite"
    if dry_run:
        store: Any = OverlayStore(SqliteStore(str(db)) if db.exists() else None)
    else:
        Path(st.state_dir).mkdir(parents=True, exist_ok=True)
        store = SqliteStore(str(db))
    odoo = None
    if need_odoo:
        from .odoo_ticket_watch import open_reader
        odoo = open_reader(st)
    return Ctx(st=st, store=store, queue=TeamhubQueue(st, dry_run=dry_run, store=store), threads=ThreadMap(store),
               approvals=ApprovalBook(store, st), odoo=odoo, dry_run=dry_run)


def _clear_failed(ctx: Ctx, key: str) -> bool:
    m = re.match(r"t(\d+)-", key)
    if m and ctx.threads.get(int(m.group(1))):
        tid = int(m.group(1))
        ctx.threads.patch(tid, failed="")
        mark = ctx.store.get("teamhub_state", "watch") or {"last_id": 0}
        ctx.store.put("teamhub_state", "watch", {"last_id": min(int(mark["last_id"]), tid - 1)})   # look at it again
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="teamhub_threads")
    ap.add_argument("command", choices=("watch-once", "process-submissions", "scan-replies", "clear-unknown", "repost-card"))
    ap.add_argument("--ticket", type=int, default=0, help="repost-card: the Odoo ticket id")
    ap.add_argument("--key", default="", help="clear-unknown: the queue key an operator has checked was NOT posted")
    ap.add_argument("--dry-run", action="store_true", help="read only; print what would be queued; write nothing")
    ap.add_argument("--config", default="config.yaml")
    a = ap.parse_args(argv)
    try:
        st = load_settings(a.config)
        with contextlib.nullcontext() if a.dry_run else single_instance(st.state_dir):
            ctx = build_ctx(st, dry_run=a.dry_run, need_odoo=a.command == "watch-once")
            if a.command != "clear-unknown":
                ctx.queue.check_gateway()                     # one check up front: a dead gateway refuses the whole run
            if a.command == "clear-unknown":
                print("cleared" if ctx.queue.clear_marker(a.key) and _clear_failed(ctx, a.key) else "no such marker")
            elif a.command == "repost-card":
                print(json.dumps(repost_card(ctx, a.ticket), ensure_ascii=False))
            elif a.command == "watch-once":
                from .odoo_ticket_watch import watch_once
                print(json.dumps(watch_once(ctx), ensure_ascii=False))
            elif a.command == "scan-replies":
                print(json.dumps(scan_pending(ctx), ensure_ascii=False))
            else:
                print(json.dumps(process_all(ctx), ensure_ascii=False))
    except (ConfigError, Refused, OSError, KeyError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
