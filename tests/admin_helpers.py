"""In-process client for the admin WSGI app (no sockets) + a demo-seeded app. Not collected as tests."""
from __future__ import annotations

import io
import re
from http.cookies import SimpleCookie
from urllib.parse import urlencode

from twin_agent.hub.admin import auth, seed
from twin_agent.hub.admin.app import AdminApp
from twin_agent.hub.admin.auth import RateLimiter
from twin_agent.hub.admin.config import AdminConfig, StaffUser
from twin_agent.hub.admin.graph import FakeGraphClient



def fast_hash(password: str) -> str:
    """A scrypt hash at a LOW cost for fixture users only (verify honours the cost stored in the hash), so the suite
    does not spend a second per login. The production cost is asserted separately (test_password_hash_...)."""
    old, auth._N = auth._N, 2 ** 10
    try:
        return auth.hash_password(password)
    finally:
        auth._N = old


PW = "correct horse battery"
OTHER_PW = "another pass phrase"
OUTSIDER_ID = 4242            # a staff user whose approver id the hub does not know


class Client:
    def __init__(self, app: AdminApp, ip: str = "127.0.0.1", env: dict | None = None):
        self.app, self.ip, self.cookies, self.env = app, ip, {}, env or {}

    def request(self, method: str, path: str, form: dict | None = None, *, cookies: dict | None = None):
        body = urlencode(form or {}).encode()
        path, _, qs = path.partition("?")
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": qs, "REMOTE_ADDR": self.ip,
               "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body), "HTTP_HOST": "127.0.0.1:8780",
               "HTTP_COOKIE": "; ".join(f"{k}={v}" for k, v in (self.cookies if cookies is None else cookies).items())}
        env.update(self.env)
        out = {}

        def start(status, headers):
            out["status"], out["headers"] = int(status.split()[0]), headers

        out["body"] = b"".join(self.app(env, start)).decode("utf-8")
        for k, v in out["headers"]:
            if k == "Set-Cookie":
                c = SimpleCookie(v)
                for name, m in c.items():
                    if m.value == "" or m["max-age"] == "0":
                        self.cookies.pop(name, None)
                    else:
                        self.cookies[name] = m.value
        out["h"] = {k: v for k, v in out["headers"] if k != "Set-Cookie"}
        out["set_cookies"] = [v for k, v in out["headers"] if k == "Set-Cookie"]
        return type("R", (), out)

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, form):
        return self.request("POST", path, form)

    def csrf(self, path: str = "/tickets") -> str:
        return re.search(r'name="csrf" value="([^"]+)"', self.get(path).body).group(1)

    def login(self, username="demo-approver", password=PW):
        pre = re.search(r'name="csrf" value="([^"]+)"', self.get("/login").body).group(1)
        return self.post("/login", {"csrf": pre, "username": username, "password": password})


def make_app(**over):
    hub = seed.build_demo_hub(":memory:")
    info = seed.seed(hub)
    users = {"demo-approver": StaffUser("demo-approver", "Demo Approver", seed.DEMO_APPROVER_ID, fast_hash(PW)),
             "outsider": StaffUser("outsider", "Outside Person", OUTSIDER_ID, fast_hash(OTHER_PW))}
    cfg = AdminConfig(users=users, app_id=over.pop("app_id", "123"), config_id=over.pop("config_id", "456"),
                      cookie_secure=over.pop("cookie_secure", True), **over.pop("cfg", {}))
    graph = over.pop("graph", None) or FakeGraphClient()
    app = AdminApp(hub, cfg, graph, limiter=over.pop("limiter", None) or RateLimiter(), **over)
    return app, hub, info


def logged_in(**over):
    app, hub, info = make_app(**over)
    c = Client(app)
    assert c.login().status == 303
    return c, hub, info


def pending_for(hub, tid):
    st = hub.ticket(tid)
    return hub.store.get("approvals", st.pending_nonce)


def approve_form(c, hub, tid, action="approve", text=None):
    doc = pending_for(hub, tid)
    f = {"csrf": c.csrf(f"/tickets/{tid}"), "nonce": doc["nonce"], "draft_hash": doc["draft_hash"], "action": action}
    if text is not None:
        f["text"] = text
    return f


def approve_form_safe(c, hub, tid):
    """A form for a ticket that may have NO live approval any more (a replay attempt): stale values are fine."""
    return {"csrf": c.csrf(f"/tickets/{tid}"), "nonce": "x", "draft_hash": "x", "action": "approve"}
