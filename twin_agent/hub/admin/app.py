"""The WSGI app. ``AdminApp(hub, config, graph)`` is a plain callable: tests drive it with synthetic environ dicts.

Approval = ``Hub.on_owner_decision(nonce, <the logged-in user's approver id>, text=<edit or None>)`` and nothing
else. Single use, approver-bound, draft-bound, window re-check and the actual send all live in the hub; this page
adds only the guard that the draft the person SAW is the draft still pending (``draft_hash``)."""
from __future__ import annotations

import re
import secrets
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import auth, data, proxy, seed, views
from .config import AdminConfig
from .graph import SYNC_TYPES, GraphClient, GraphError, LocalRefusal, MetaRefused
from .meta_store import BusinessStore, SignupStates, valid_id, valid_slug
from .own_waba import OWN_SLUG
from .web import BASE_CSP, STATUS_TEXT, Request, Response, redirect

STATIC = Path(__file__).parent / "static"
SESSION_COOKIE, PRE_COOKIE = "aa_session", "aa_pre"
_PRE_SHAPE = re.compile(r"[A-Za-z0-9_-]{32}")        # what ``secrets.token_urlsafe(24)`` produces


def connect_csp(cfg: AdminConfig) -> str:
    """The /connect page posts a form that redirects to the launcher origin; Chrome applies ``form-action`` to that
    redirect, so the launcher origin (and only it) is allowed there. No SDK, no external script, no connect-src."""
    u = urlsplit(cfg.launcher_url)
    origin = f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else ""
    return BASE_CSP.replace("form-action 'self'", f"form-action 'self' {origin}".rstrip())


class AdminApp:
    def __init__(self, hub, config: AdminConfig, graph: GraphClient, *, sessions=None, limiter=None,
                 signups: SignupStates | None = None, businesses: BusinessStore | None = None,
                 allow_live: bool | None = None):
        live = config.allow_live if allow_live is None else allow_live
        if not live and not seed.is_demo_db(hub):
            from ..production import is_production_hub    # lazy: both runners import this package
            from ..staging import is_staging_hub
            if not (is_staging_hub(hub) or is_production_hub(hub)):
                raise RuntimeError("AdminApp refuses a hub that is not the demo hub or a validated staging hub (or the production hub): "
                                   "approvals here send real WhatsApp messages from a web login. Set allow_live "
                                   "(config) only after the PDPL sign-off.")
        self.hub, self.cfg, self.graph = hub, config, graph
        self.access_users: dict | None = None             # verified Access email -> the ONE hub username it may use
        self.sessions = sessions or auth.SessionStore()
        self.limiter = limiter or auth.RateLimiter()
        self.signups, self.businesses = signups, businesses
        self.trusted = proxy.parse_networks(config.trusted_proxies)
        self._approve_lock = threading.Lock()             # one approval at a time: threaded server, one hub

    # ---------------------------------------------------------------- WSGI entry

    def __call__(self, environ, start_response):
        req = None
        try:
            req = Request(environ, self.trusted)
            resp = self.handle(req)
        except Exception:                                   # noqa: BLE001 - never leak a traceback to the browser
            resp = Response(500, "<p>Something went wrong. Check the ticket and WhatsApp before trying again.</p>")
        if req is not None and (req.https or self.cfg.cookie_secure):
            resp.secure_cookies()
        if req is not None and req.https and self.cfg.hsts:
            resp.hsts()
        start_response(f"{resp.status} {STATUS_TEXT.get(resp.status, 'Error')}", resp.headers)
        return [resp.body]

    def handle(self, req: Request) -> Response:
        if not proxy.host_allowed(req.host, self.cfg.allowed_hosts):
            return Response(400, "Bad host")
        if req.spoofed_proxy:           # Cloudflare headers from a peer we do not trust: spoofing, or a tunnel without --trusted-proxy
            return Response(400, "Proxy headers from an untrusted peer. Start the server with --trusted-proxy.")
        if req.too_big:
            return Response(413, "Too large")
        if req.path.startswith("/static/"):
            return self.static(req.path[len("/static/"):])
        if req.path == "/favicon.ico":      # browsers ask while the login form is open: no redirect, so no new aa_pre
            return Response(404, "Not found")
        if req.path == "/login":
            return self.login(req)
        session = self.sessions.get(req.cookies.get(self._cn(req, SESSION_COOKIE), ""))
        if session is None:
            return redirect("/login")
        user = self.cfg.users.get(session.username)
        if user is None:
            return redirect("/login")
        if not self._email_matches(req, user.username):
            return Response(403, "This Cloudflare Access identity may not use that hub user.")
        return self.authed(req, session, user)

    def _email_matches(self, req: Request, username: str) -> bool:
        """Behind Access, the verified email must map to exactly this hub user (``access_users``). Local requests pass."""
        if self.access_users is None or not req.access_email:
            return True
        return self.access_users.get(req.access_email) == str(username).lower()

    # ---------------------------------------------------------------- static + login

    def _cn(self, req: Request, base: str) -> str:
        """``__Host-`` prefix whenever the cookie is Secure: the browser then refuses a cookie set by a sibling
        sub-domain (cookie tossing) because ``__Host-`` forbids a Domain attribute."""
        return ("__Host-" if (req.https or self.cfg.cookie_secure) else "") + base

    def static(self, name: str) -> Response:
        types = {"admin.css": "text/css; charset=utf-8"}
        if name not in types:
            return Response(404, "Not found")
        return Response(200, (STATIC / name).read_bytes(), types[name])

    def _login_form(self, req: Request, error: str = "", status: int = 200, *, rotate: bool = False) -> Response:
        old = req.cookies.get(self._cn(req, PRE_COOKIE), "")
        tok = old if (_PRE_SHAPE.fullmatch(old) and not rotate) else secrets.token_urlsafe(24)    # keep the token the open form holds
        r = Response(status, views.login_page(self.cfg, tok, error))
        r.cookie(self._cn(req, PRE_COOKIE), tok, max_age=600)
        return r

    def _login_name(self, typed: str) -> str:
        """The username field also takes the work email: an exact (case-insensitive) ``access_users`` email maps to its
        ONE hub username. An email that is not mapped stays as typed, so it fails exactly like a wrong username."""
        name = typed.strip().lower()
        if "@" in name and self.access_users:
            return {str(k).strip().lower(): str(v).lower() for k, v in self.access_users.items()}.get(name, name)
        return name

    def login(self, req: Request) -> Response:
        if req.method == "GET":
            return self._login_form(req)
        if req.method != "POST":
            return Response(405, "Method not allowed")
        if not auth.tokens_equal(req.form.get("csrf", ""), req.cookies.get(self._cn(req, PRE_COOKIE), "")):
            return self._login_form(req, "The form expired. Try again.", 403)
        name = self._login_name(req.form.get("username", ""))
        if not self._email_matches(req, name):
            return self._login_form(req, "That Cloudflare Access identity may not log in as this user.", 403, rotate=True)
        if self.limiter.blocked(req.ip, name):
            return self._login_form(req, "Too many attempts. Wait a few minutes.", 429)
        user = auth.check_login(self.cfg.users, name, req.form.get("password", ""))
        if user is None:
            self.limiter.fail(req.ip, name)
            return self._login_form(req, "Wrong username or password.", 403, rotate=True)
        self.limiter.reset(req.ip, name)
        self.sessions.destroy(req.cookies.get(self._cn(req, SESSION_COOKIE), ""))
        s = self.sessions.create(user.username)                 # a NEW session id at every login
        r = redirect("/tickets")
        r.cookie(self._cn(req, SESSION_COOKIE), s.sid)
        r.cookie(self._cn(req, PRE_COOKIE), "", max_age=0)
        return r

    # ---------------------------------------------------------------- authenticated routes

    def authed(self, req: Request, session, user) -> Response:
        if req.method == "POST":
            if not auth.tokens_equal(req.form.get("csrf", ""), session.csrf):
                return Response(403, views.layout("Form expired", views._flash("csrf"), cfg=self.cfg, user=user,
                                                  csrf=session.csrf))
            return self.post(req, session, user)
        if req.method != "GET":
            return Response(405, "Method not allowed")
        return self.get(req, session, user)

    def get(self, req: Request, session, user) -> Response:
        cfg, csrf, p = self.cfg, session.csrf, req.path
        if p == "/":
            return redirect("/tickets")
        if p == "/tickets":
            return Response(200, views.tickets_page(cfg, user, csrf, data.ticket_rows(self.hub)))
        if p.startswith("/tickets/"):
            return self.ticket(req, session, user)
        if p == "/holds":
            return Response(200, views.holds_page(cfg, user, csrf, data.hold_rows(self.hub)))
        if p == "/connect":
            ready = bool(self.signups and cfg.launcher_url and cfg.app_id and cfg.config_id)
            pub = self.businesses.public() if self.businesses else []
            return Response(200, views.connect_page(cfg, user, csrf, pub, ready=ready, flash=req.query.get("r", "")),
                            csp=connect_csp(cfg))
        if p == "/numbers":
            return Response(200, self._numbers(user, csrf, req.query.get("r", "")))
        if p == "/templates":
            return Response(200, self._templates(user, csrf))
        return Response(404, "Not found")

    def post(self, req: Request, session, user) -> Response:
        if req.path == "/logout":
            self.sessions.destroy(session.sid)
            r = redirect("/login")
            r.cookie(self._cn(req, SESSION_COOKIE), "", max_age=0)
            return r
        if req.path == "/connect/start":
            return self.connect_start(req)
        if req.path in ("/meta/subscribe", "/meta/sync"):
            return self.meta_write(req)
        if req.path == "/meta/pick":
            return self.pick_number(req)
        parts = req.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "holds" and parts[2] == "done":
            return self.done_hold(req, parts[1], user)
        if len(parts) == 3 and parts[0] == "tickets" and parts[1].isascii() and parts[1].isdigit() and parts[2] == "approve":
            return self.approve(req, int(parts[1]), user)
        return Response(404, "Not found")

    def _numbers(self, user, csrf: str, flash: str = "") -> str:
        try:
            return views.numbers_page(self.cfg, user, csrf, self.graph.list_wabas(), flash=flash)
        except GraphError as e:
            return views.numbers_page(self.cfg, user, csrf, None, str(e), flash=flash)

    # ---------------------------------------------------------------- Embedded Signup start + explicit Meta writes

    def connect_start(self, req: Request) -> Response:
        """Mint a single-use state (logged-in admins only) and send the browser to the launcher origin."""
        cfg, back = self.cfg, "/connect?r="
        if not (self.signups and cfg.launcher_url and cfg.app_id and cfg.config_id):
            return redirect(back + "connect_off")
        business = req.form.get("business", "").strip().lower()
        if not valid_slug(business) or business == OWN_SLUG:       # OWN_SLUG is the read-only own-number entry
            return redirect(back + "connect_bad")
        if self.businesses:
            self.businesses.reset_claims(business)          # a new attempt starts without the last attempt's claims
        state = self.signups.new(business)
        if state is None:
            return redirect(back + "connect_busy")
        return redirect(f"{cfg.launcher_url.rstrip('/')}/?s={quote(state)}")

    def meta_write(self, req: Request) -> Response:
        """subscribe webhooks / start the coexistence sync: ONE Graph POST per click, never automatic, never retried.
        Off unless ``allow_meta_writes`` (the owner must OK it first)."""
        back, f = "/numbers?r=", req.form
        if not self.cfg.allow_meta_writes:
            return redirect(back + "meta_disabled")
        biz = f.get("business", "")                         # the write uses THIS business's own token, never one found by id
        try:
            if req.path == "/meta/subscribe":
                ok = f.get("confirm") == "yes" and valid_slug(biz) and valid_id(f.get("waba_id", ""))
                if ok:
                    self.graph.subscribe_app(f["waba_id"], biz)
            else:
                ok = (f.get("confirm") == "yes" and valid_slug(biz) and valid_id(f.get("phone_number_id", ""))
                      and f.get("sync_type") in SYNC_TYPES)
                if ok:
                    self.graph.start_sync(f["phone_number_id"], f["sync_type"], biz)
        except LocalRefusal:
            return redirect(back + "meta_not_connected")     # refused here: nothing was sent to Meta
        except MetaRefused:
            return redirect(back + "meta_refused")           # Meta said no: nothing changed
        except GraphError:
            return redirect(back + "meta_failed")            # timeout / network: UNCERTAIN, check in Meta
        return redirect(back + ("meta_ok" if ok else "meta_bad"))

    def pick_number(self, req: Request) -> Response:
        """Choose which number of a connected account this business uses (needed for the history sync). Local only: it
        changes our record, not Meta. The id must be one Graph lists for this business's own WABA."""
        back, f = "/numbers?r=", req.form
        biz, phone = f.get("business", ""), f.get("phone_number_id", "")
        if not (self.businesses and valid_slug(biz) and biz != OWN_SLUG and valid_id(phone)):
            return redirect(back + "pick_bad")
        try:
            listed = [w for w in self.graph.list_wabas() if w.get("business") == biz and not w.get("error") and not w.get("own")]
        except GraphError:
            return redirect(back + "pick_bad")
        if not any(n.get("id") == phone for w in listed for n in w.get("phone_numbers", [])):
            return redirect(back + "pick_bad")
        self.businesses.save(biz, {"phone_number_id": phone})
        return redirect(back + "picked")

    def _templates(self, user, csrf: str) -> str:
        try:
            wabas = self.graph.list_wabas()
        except GraphError as e:
            return views.templates_page(self.cfg, user, csrf, None, str(e))
        items, errors = [], []
        for w in wabas:                                     # one business failing must not hide the others
            try:
                if w.get("error"):
                    raise GraphError(w["error"])
                items += [{**t, "waba": w.get("name")} for t in self.graph.list_templates(w["id"], w.get("business", ""))
                          if str(t.get("status")).upper() == "APPROVED"]
            except GraphError as e:
                errors.append(f"{w.get('name')}: {e}")
        return views.templates_page(self.cfg, user, csrf, items, " | ".join(errors))

    def ticket(self, req: Request, session, user) -> Response:
        tail = req.path[len("/tickets/"):]
        state = self.hub.ticket(int(tail)) if tail.isascii() and tail.isdigit() else None
        if state is None:
            return Response(404, "Not found")
        d = data.ticket_detail(self.hub, state, self.cfg)
        return Response(200, views.ticket_page(self.cfg, user, session.csrf, d, req.query.get("r", "")))

    # ---------------------------------------------------------------- approval (via the existing hub path)

    def done_hold(self, req: Request, hold_id: str, user) -> Response:
        """A person dealt with a hold: the hub closes it (and audits who) so ``open_holds`` goes down."""
        done = getattr(self.hub, "mark_hold_done", None)
        if done is not None and hold_id.isalnum():
            done(hold_id, hub_user=user.username, access_email=req.access_email)
        return redirect("/holds")

    def _audit_approval(self, req: Request, user, tid: int, res: dict) -> None:
        """Who approved: the hub user, and (behind Cloudflare Access) the verified email. Ids and names only, no text."""
        from ..store import audit_append
        try:
            audit_append(self.hub.store, {"role": "approver", "model": "approval", "method": "approve", "ticket_id": int(tid),
                                          "hub_user": user.username, "access_email": req.access_email,
                                          "outcome": "ok" if res.get("ok") else "refused"})
        except Exception:                                       # noqa: BLE001 - the audit must never hide the decision
            pass

    def approve(self, req: Request, tid: int, user) -> Response:
        back = f"/tickets/{tid}?r="
        state = self.hub.ticket(tid)
        if state is None:
            return Response(404, "Not found")
        doc = data.live_draft(self.hub, state)
        f = req.form
        if (doc is None or doc["nonce"] != f.get("nonce") or doc["nonce"] != state.pending_nonce
                or not auth.tokens_equal(doc["draft_hash"], f.get("draft_hash", ""))):
            return redirect(back + "stale")                  # the draft the person saw is not the pending one
        text = None
        if f.get("action") == "edit_approve" and doc["kind"] in data.CLIENT_KINDS:
            edited = f.get("text", "").replace("\r\n", "\n").replace("\r", "\n").strip()
            if not edited:                                  # never hand the hub an empty edit
                return redirect(back + "empty_edit")
            text = edited if edited != doc["draft"].strip() else None   # an unchanged box is NOT an edit
        with self._approve_lock:
            res = self.hub.on_owner_decision(doc["nonce"], user.approver_id, approved=True, text=text, by=user.username,
                                             by_name=user.display_name)
        self._audit_approval(req, user, tid, res)
        return redirect(back + quote(_outcome(res)))


def _outcome(res: dict) -> str:
    if res.get("ok"):
        return "sent" if res.get("sent") else "done"
    reason = str(res.get("reason") or "")
    return reason if reason in views.FLASH else ("stale" if reason in ("used", "expired", "unknown", "draft_mismatch")
                                                 else "failed")

