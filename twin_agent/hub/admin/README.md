# Hub admin page (Meta App Review screencast surface)

A small web page over the WhatsApp hub: Login, Connect WhatsApp, Connected numbers, Templates, Tickets (with approvals).
Python stdlib WSGI (`wsgiref`, threaded, with timeouts). `httpx` (the `live` extra) is needed only for `--graph real` and
for the launcher's code exchange. The hub behind it is still the all-fakes DEMO hub: fictional tickets, fake sender.

## Run the demo locally (fake data, no network)

```
python -m twin_agent.hub.admin seed  --db demo.db        # fictional "Demo Bakery" / "Test Customer" tickets
python -m twin_agent.hub.admin serve --db demo.db        # http://127.0.0.1:8780/
```

`seed` generates a random password for user `demo-approver` and stores only its scrypt hash in
`~/.claude/secrets/aa-admin-demo-users.json` (override with `--users-file`). **The password is printed only to a real
terminal.** From anything else (an agent, a pipe, a log) it is written to `aa-admin-demo-users.password.txt` next to the
users file (mode 0600; on Windows the profile ACL on `~/.claude/secrets` is the protection) and only the path is printed.
Read it yourself and delete the file after the recording. Other staff users come from a JSON file outside git:
`python -m twin_agent.hub.admin hash-password` prints a hash to paste into it.

`serve` refuses a SQLite file that `seed` did not create, binds 127.0.0.1 only (a public bind needs
`--allow-public-bind` AND at least one `--allowed-host`), and shows a banner.

## Hardening (issue #63)

| Concern | What the code does |
|---|---|
| Behind a Cloudflare Tunnel | `--trusted-proxy CIDR` (repeatable; REQUIRED whenever a public `--allowed-host` or an https `--launcher-url` is given: `serve` refuses to start without it, and a request carrying `CF-Connecting-IP`/`CF-Visitor` from an untrusted peer gets 400; local `cloudflared` is `127.0.0.1/32`). `CF-Connecting-IP`, `X-Forwarded-Proto` and `CF-Visitor` are believed ONLY when the TCP peer is inside a trusted CIDR; from any other peer they are ignored (a forged header cannot dodge the limiter). Default: nobody trusted. |
| Host allowlist | `--allowed-host HOST` (repeatable) plus loopback `127.0.0.1:PORT`, `localhost:PORT`, `[::1]:PORT` on a loopback bind. Case-insensitive; a Host with `@`, `/` or spaces is refused. A public bind without one is refused. |
| HTTPS | A request that arrived over HTTPS (directly, or via a trusted proxy) gets `Secure` on every cookie (named `__Host-aa_session` / `__Host-aa_pre`, so a sibling sub-domain cannot toss a cookie) and `Strict-Transport-Security: max-age=31536000`. No HSTS over plain http. |
| Login limiter | 5 failures / 5 min per (IP, username) pair, 15 / 5 min per IP. No per-username counter shared across addresses and no global counter, so a stranger cannot lock the real approver out. At most 4096 keys are held (oldest evicted); expired keys are dropped. |
| Demo-only guarantee in the app | `AdminApp` raises unless the hub carries the demo marker, or `allow_live` is set (config). `allow_live` is not exposed on the CLI: the only hub the CLI can build is the demo hub. Putting a real hub behind this page is on the PDPL checklist. |
| Server | `server.make_threaded_server`: one daemon thread per request, at most 32 at once (extra connections are closed), 15 s socket timeout, no traceback or peer on stderr. One slow or idle client no longer stalls the page. |
| Wording | A failure after an approval says "Something failed after approval. Check the ticket and WhatsApp before retrying: the message may already have gone out." Unknown outcomes and the 500 page are neutral. "Nothing was sent" appears only where the hub was never called. |

## Embedded Signup: a separate origin

The Facebook SDK (`connect.facebook.net/sdk.js`) has full same-origin power in the page that loads it and cannot be
pinned with SRI. Loaded in the admin origin it could ride a staff session (fetch the ticket page, read the CSRF token and
the draft hash, POST an approval). Two designs were possible: (a) a strict CSP on `/connect` only, or (b) a **separate
minimal app on its own origin**. A CSP alone cannot stop a script that is allowed to run on the page from reading
other same-origin pages (review L1 showed `connect-src` is not a complete answer), so this change does (b):

* `launcher.py` is its own WSGI app on its own port and **its own host name**. It serves exactly: the launcher page
  (`GET /?s=<state>`), `POST /callback`, and two static files. It sets **no cookie**, renders no stored data, has no
  login, no ticket, no token to read. The admin session cookie is host-scoped, so the browser never sends it there.
* The admin `/connect` page loads **no external script**. It only mints a single-use state (`POST /connect/start`, CSRF
  protected, logged-in staff only) and redirects the browser to `launcher_url/?s=<state>`. Its CSP allows that one extra
  `form-action` origin and nothing else.
* `serve` refuses a launcher whose host name equals an admin host name. Do not put both on `localhost` with different
  ports: **cookies ignore ports**.
* Callback checks (same as `embedded-signup/server.py`): server-generated single-use state (20 min, once for the session
  info, once for the code, at most 100 pending), `Origin == scheme://Host`, JSON only, 16 KB cap, per-IP limit. The code is
  exchanged at once (it lives 30 seconds) with the app secret from `--app-secret-file`
  (default `~/.claude/secrets/meta-acme-app.json`, `{"app_id","app_secret"}`, must match `--app-id`). Secret and code
  travel in the POST body, never a URL. **The one URL exception is Graph `debug_token`**, whose `input_token` is a query
  parameter by Meta's design (the call carries the app token in the header). It goes only to graph.facebook.com over TLS;
  our code logs nothing, and the `httpx` / `httpcore` loggers are set to WARNING with a filter that drops any record
  mentioning `input_token` (httpx would otherwise log the full URL at INFO). A test proves the token never appears in logs
  or tracebacks, and that without the filter it would.
* The token is filed in `--business-dir` (default `~/.claude/secrets/aa-meta-businesses/<business>.json`, 0600, atomic
  write): `waba_id`, `phone_number_id` (VERIFIED, see below), `business_token`, plus `claimed_*` display-only ids. Not in the hub DB, not in git, not in logs; the
  launcher, the Graph client and the store contain no `print` or `logging`. No page ever renders the token. The ids from
  Meta's `postMessage` are the BROWSER's claim: digits-only, stored as `claimed_*`, never used to pick a token. After the code
  exchange the launcher calls Graph `debug_token` (app access token in a header; `granular_scopes` target ids of
  `whatsapp_business_management`) and then lists the WABA's numbers with the business's own token, and stores ONLY the ids
  the token really grants (a claim outside that set is ignored; no usable claim means exactly one granted WABA or a refusal).
  An id belongs to one business: a second business trying to hold it gets 409 and nothing is written. Writes and reads
  resolve the token by business name (the write forms post it), never by scanning ids; `token_by_id` raises on 0 or more
  than 1 match. One failing business shows a row-level error instead of blanking the pages.

## Real Graph client (`--graph real`)

`graph_real.RealGraphClient`: Graph **v27.0**, `httpx`, one `Authorization: Bearer` header (never a URL, except `debug_token`'s `input_token`, see above). Reads, all GET:
WABA name, phone numbers (status, quality rating), whether OUR app is in `subscribed_apps` ("Webhooks: subscribed"), and
message templates (body from the BODY component). Every error is a fixed sentence plus at most the numeric HTTP status and
Meta error code; a response body is never shown. Timeouts 15 s (5 s connect), redirects off, **no retries**.

Where the token comes from: the per-business store above (the Embedded Signup result), read per call through
`BusinessStore` (`records`, `token_for`, `token_by_id`, `save`, `public`). **This is a deliberate divergence from "use the
hub's vault"**: `hub/creds.py` has a fixed list of names bound to one Windows user (Credential Manager + DPAPI; `dpapi`
raises off win32), while these records are dynamic (one per connected business) and the stg server is not that Windows
user. So they live in the secrets directory (`~/.claude/secrets/...`, outside git; on Windows 0600 does nothing, the
profile ACL is the protection). A vault-backed `BusinessStore` is a drop-in replacement when the server vault exists;
the hub's `whatsapp-cloud` vault name is the sender's single token and is deliberately not touched here.

Default is `--graph fake` (fictional Demo Bakery data).

### Meta write actions: off, explicit, one click each

Connecting does **not** subscribe webhooks, register a number or start a history sync. The only write paths are two
buttons on Connected numbers, each a single Graph POST with a confirm tick, CSRF, and **disabled unless
`--allow-meta-writes` (config `allow_meta_writes`) is set**. The server refuses the POST too, so a hand-made request
cannot bypass the disabled button.

* **Subscribe webhooks**: `POST /{waba_id}/subscribed_apps`.
* **Start history sync / contacts sync**: `POST /{phone_number_id}/smb_app_data` with `sync_type` `history` or
  `smb_app_state_sync`. **Coexistence: the history sync must start within 24 hours of the business finishing Embedded
  Signup, or the business has to offboard and redo the flow.**
* There is no "register number" action: a coexistence number is already registered, and registering it is wrong.

**Sysadmin must OK each of these before `--allow-meta-writes` is ever used**, and the webhook endpoint must exist first
(Meta requires a working webhook for coexistence).

## Preconditions that are NOT code (do these before real data or a public tunnel)

* **Cloudflare Access (or 2FA) in front of the admin login.** A single-factor password is not enough for approvals that
  send real WhatsApp messages. Put Access on the admin hostname only, never on the launcher (Meta's popup must reach it).
* **Per-business access control** before a second real business: today any logged-in staff user sees every ticket.
* **PDPL sign-off** and the hub's own go-live gates. No real client data until then.
* A real Hub factory with `allow_live` (does not exist yet; the CLI can only run the demo hub).

## Recording the App Review screencast

Staging or a local run on TEST data only. Nothing here sends a WhatsApp message to a real client, and nothing in this
page calls Meta unless you start the flow.

0. **Try the code exchange once on the TEST app before recording.** `embedded-signup/server.py` used a POST for
   `oauth/access_token`; Meta's docs also show GET. Do one dry run with a test user and confirm Meta accepts the POST (the
   launcher says "Something went wrong" otherwise); if it does not, tell the owner: a GET would put the code and the app secret in the URL, so do not
   switch without a security look.
0. **Known blocker (13 Nov):** until ACME LLC is a verified **Tech Provider** (business verification + advanced access
   to `whatsapp_business_management` / `whatsapp_business_messaging`), Meta's popup stops at "Acme can't onboard
   customers right now". Check this before booking recording time (`embedded-signup/README.md`).
1. **Prerequisites (human):** the Meta app id and Embedded Signup configuration id; `~/.claude/secrets/meta-acme-app.json`
   with `{"app_id","app_secret"}` (same app id); a Meta test user that admins a test business with a test WABA/number;
   `pip install -e ".[live]"` (httpx). In Meta App Settings add the launcher host to *Valid OAuth Redirect URIs* and
   *Allowed Domains for the JavaScript SDK*.
2. **Seed once, by a human at a terminal:** `python -m twin_agent.hub.admin seed --db demo.db` (prints the password).
3. **Two public hostnames, one per origin.** Quick-tunnel stopgap (names change at every restart, so re-add the launcher
   host in Meta each time):
   ```
   cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8780     # admin  -> https://ADMIN.trycloudflare.com
   cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8781     # launcher -> https://LAUNCH.trycloudflare.com
   ```
   On `twin-stg` use two real hostnames instead (e.g. `staging.example.com` for admin, `connect.staging.example.com` for the
   launcher) with Cloudflare Access on the admin one.
4. **Start:**
   ```
   python -m twin_agent.hub.admin serve --db demo.db --graph real \
     --app-id <APP_ID> --config-id <CONFIG_ID> \
     --allowed-host ADMIN.trycloudflare.com --trusted-proxy 127.0.0.1/32 \
     --launcher-url https://LAUNCH.trycloudflare.com --launcher-port 8781
   ```
   The banner reads "TEST MODE: fake hub data. Meta reads are REAL (Graph v27.0). Meta writes are OFF." (`--launcher-url`
   needs `--graph real`: the launcher does a real code exchange and stores a real token.)
5. **Record.** If the popup is cancelled, pressing the button again on the same launcher page works (the session info may be
   posted again until a FINISH); if the code was already used (a failed exchange) press **Connect WhatsApp** in the admin to
   get a NEW link. Several WABAs: if the token grants more than one and Meta's session info has not arrived, the launcher
   keeps the token in memory (never on disk) until it does or the link expires (20 min); the page then says "Almost done".
   A number that cannot be verified is saved as empty and the page says "pick the number": open **Connected numbers**
   and press **Use this number for the sync** on the right row (a local choice, not a Meta write); only the selected
   number offers **Start history sync** (the 24-hour coexistence rule). First open the browser DevTools console on the launcher page and press the button once: the launcher CSP
   (strict, but `style-src` allows inline) was written without a live browser, so confirm there are no "Refused to ..."
   violations from `connect.facebook.net` or the Facebook iframe. If there are, relax only the launcher's CSP
   (`launcher.LAUNCHER_CSP`), never the admin's. Then (scenes 1-7 of `app-review.md` section 4): log in as `demo-approver` on the admin address; **Connect WhatsApp**
   (enter a business name such as `demo-bakery`) opens the launcher; complete Meta's popup with the test user; back on the
   admin address open **Connected numbers** (WABA, number, status, quality, "Webhooks: NOT subscribed") and **Templates**.
   The ticket/approval scenes use the fake hub and fake sender, so say so in the captions ("demo data").
6. **Webhooks line (scene 6 says "subscribed"):** the page shows "NOT subscribed" until a human, with Sysadmin's OK, restarts
   with `--allow-meta-writes` and clicks **Subscribe webhooks** once on the test WABA. Without that OK, either leave the
   line out of the recording or show the honest "NOT subscribed" state.
7. **After recording:** stop both `serve` and both `cloudflared` processes, delete `aa-admin-demo-users.password.txt`, and
   delete the test business file in `--business-dir` (it holds a token).

**Do NOT enable:** `--allow-meta-writes` (without Sysadmin's OK), `--allow-public-bind`, a real hub or real client numbers, any
Cloudflare hostname shared between admin and launcher, the production Meta app on any real business, or a trusted proxy
that is not your own `cloudflared`. History sync is **not** needed for the review video; if a real coexistence number is
ever connected, the 24-hour sync clock starts at that moment.

## How approval works (no parallel path)

The Approve buttons call `Hub.on_owner_decision(nonce, <logged-in user's approver id>, text=<edit or None>)`, the same
call the Telegram tap makes. Single use, approver binding, draft-hash binding, expiry, edit re-scan and re-judge, the
24h window re-check and the send all happen inside the hub. The page only adds a guard that the draft the person saw
is still the pending one. The page never calls the WhatsApp sender. Approvals are serialised by one lock.

## Still faked / not built

- The hub is the all-fakes demo hub (no production Hub factory). Single business only: no tenancy.
- Delivery status: the hub does not ingest Meta status callbacks yet; the demo sender writes a simulated "delivered".
- Approver ids are the hub's existing approver ids (Telegram ids today); a staff user maps to one id in the users file.
- The ack row in the timeline has no time: the hub does not record per-stage timestamps.
- Not changed by this work (still open on #63): the older approvals lacking `judge_flag` fallback.
