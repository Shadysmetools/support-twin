"""Setup/provision interview: scans what's satisfied, stores secrets in the vault + consents in a status file,
and asks only for what's still missing."""
from __future__ import annotations

from twin_agent import creds
from twin_agent.onboarding import setup


def _answers() -> dict[str, str]:
    return {
        "odoo-sys-principal": '{"login":"principal","api_key":"k"}',
        "tg-bot-token": "123:abc",
        "wiki": "kc-key",
        "elevenlabs": "el-key",
        "groq": "gq-key",
        "google-oauth-client": '{"client":"x"}',
        "d2_voice": "C:/rec/principal.wav",
        "avatar_consent": "yes",
        "qa_user": "qa_principal",
        "shopify_sandboxed": "confirmed",
    }


def test_scan_all_missing_initially(tmp_home, fake_keyring):
    scanned = setup.scan(backend=fake_keyring)
    assert len(scanned) == len(setup.REQUIREMENTS)
    assert all(not ok for _, ok in scanned)


def test_run_setup_stores_secrets_in_vault_and_consents_in_status(tmp_home, fake_keyring):
    summary = setup.run_setup(_answers(), backend=fake_keyring)
    # secrets -> vault
    assert creds.has_secret("wiki", backend=fake_keyring)
    assert creds.has_secret("elevenlabs", backend=fake_keyring)
    assert creds.has_secret("odoo-sys-principal", backend=fake_keyring)
    # consents/resources -> status file (not the vault)
    status = setup.load_status()
    assert status["qa_user"] == "qa_principal"
    assert status["shopify_sandboxed"] == "confirmed"
    assert status["d2_voice"] == "C:/rec/principal.wav"
    assert summary["missing_required"] == []


def test_skip_or_blank_leaves_item_missing(tmp_home, fake_keyring):
    a = _answers()
    a["qa_user"] = "no"            # explicit skip
    a["shopify_sandboxed"] = ""    # blank
    summary = setup.run_setup(a, backend=fake_keyring)
    assert "qa_user" in summary["missing_required"]
    assert "shopify_sandboxed" in summary["missing_required"]


def test_already_satisfied_secret_is_not_remissing(tmp_home, fake_keyring):
    creds.set_secret("wiki", "already", backend=fake_keyring)
    scanned = {r.key: ok for r, ok in setup.scan(backend=fake_keyring)}
    assert scanned["wiki"] is True


def test_interactive_asks_only_missing(tmp_home, fake_keyring):
    creds.set_secret("wiki", "already", backend=fake_keyring)  # pre-satisfied
    a = _answers()
    missing = [r for r in setup.REQUIREMENTS
               if not setup.is_satisfied(r, backend=fake_keyring, status=setup.load_status())]
    feed = iter(a[r.key] for r in missing)
    out: list[str] = []
    # interactive() also asks work-site logins after the requirements; "" skips those
    summary = setup.interactive(ask=lambda _p, _s: next(feed, ""), out=out.append, backend=fake_keyring)
    assert "wiki" not in summary["stored"]        # pre-satisfied, never re-asked
    assert summary["missing_required"] == []
    assert any("[have]" in line for line in out)


def test_store_and_load_site_login_roundtrip(tmp_home, fake_keyring):
    setup.store_site_login("helpdesk.example.com", "principal@x", "pw1", backend=fake_keyring)
    setup.store_site_login("accounts.google.com", "principal@gmail", "pw2", backend=fake_keyring)
    logins = setup.load_site_logins(backend=fake_keyring)
    assert logins["helpdesk.example.com"] == {"username": "principal@x", "password": "pw1"}
    assert logins["accounts.google.com"]["username"] == "principal@gmail"
    # both live in the ONE site-logins vault entry
    assert creds.has_secret("site-logins", backend=fake_keyring)


def test_collect_site_logins_stores_and_skips(tmp_home, fake_keyring):
    # feed matches the ask order: site1 user+pass (stored), site2 blank user (skipped)
    feed = iter(["u1", "p1", ""])
    stored = setup.collect_site_logins(ask=lambda _p, _s: next(feed, ""), backend=fake_keyring)
    sites = [s for s, _ in setup.WORK_SITES]
    assert stored == [sites[0]]
    have = setup.load_site_logins(backend=fake_keyring)
    assert sites[0] in have and sites[1] not in have


def test_missing_site_logins(tmp_home, fake_keyring):
    assert len(setup.missing_site_logins(backend=fake_keyring)) == len(setup.WORK_SITES)
    setup.store_site_login(setup.WORK_SITES[0][0], "u", "p", backend=fake_keyring)
    assert len(setup.missing_site_logins(backend=fake_keyring)) == len(setup.WORK_SITES) - 1


# ---------------------------------------------------------------- foolproofing / UX

def test_skip_word_on_a_secret_never_lands_in_the_vault(tmp_home, fake_keyring):
    a = _answers()
    a["wiki"] = "skip"          # a non-technical user types the word instead of pressing Enter
    a["elevenlabs"] = "no"
    summary = setup.run_setup(a, backend=fake_keyring)
    assert not creds.has_secret("wiki", backend=fake_keyring)
    assert not creds.has_secret("elevenlabs", backend=fake_keyring)
    assert "wiki" in summary["missing_required"]


def test_shopify_only_counts_when_exactly_confirmed(tmp_home, fake_keyring):
    a = _answers()
    a["shopify_sandboxed"] = "not yet"      # must NOT be read as confirmed (LIVE STORE safety)
    summary = setup.run_setup(a, backend=fake_keyring)
    assert "shopify_sandboxed" in summary["missing_required"]
    assert setup.load_status().get("shopify_sandboxed") is None


def test_avatar_consent_requires_explicit_yes(tmp_home, fake_keyring):
    a = _answers()
    a["avatar_consent"] = "maybe"
    summary = setup.run_setup(a, backend=fake_keyring)
    assert "avatar_consent" in summary["missing_required"]


def test_interactive_reprompts_invalid_secret_then_stores_valid(tmp_home, fake_keyring):
    # odoo-sys-principal must be a JSON object: first a bad answer, then a good one. It re-asks and stores the good.
    # Drive by a scripted queue in requirement order, with the bad+good pair for odoo first.
    feed = []
    for r in setup.REQUIREMENTS:
        if r.key == "odoo-sys-principal":
            feed += ["this is not json", _answers()[r.key]]
        else:
            feed += [_answers()[r.key]]
    it = iter(feed)
    out: list[str] = []
    summary = setup.interactive(ask=lambda _p, _s: next(it, ""), out=out.append, backend=fake_keyring)
    assert creds.has_secret("odoo-sys-principal", backend=fake_keyring)
    assert summary["missing_required"] == []
    assert any("again" in line.lower() for line in out)


def test_interactive_never_echoes_a_secret(tmp_home, fake_keyring):
    feed = []
    for r in setup.REQUIREMENTS:
        feed.append(_answers()[r.key])
    it = iter(feed)
    prompts_seen: list[str] = []

    def ask(prompt, is_secret):
        prompts_seen.append(prompt)
        return next(it, "")

    out: list[str] = []
    setup.interactive(ask=ask, out=out.append, backend=fake_keyring)
    blob = "\n".join(out) + "\n".join(prompts_seen)
    for r in setup.REQUIREMENTS:
        if r.kind == "secret":
            assert _answers()[r.key] not in blob, f"{r.key} secret leaked into output/prompts"


def test_checklist_lists_login_commands_in_dependency_order(tmp_home, fake_keyring):
    items = {it["key"]: it for it in setup.checklist(backend=fake_keyring)}
    # telegram login has a direct command
    assert items["tg-user-session"]["command"] == "python -I aa.py tg-login"
    # gmail login depends on the oauth client; with it missing, the command points at the oauth step first
    assert "creds set-file google-oauth-client" in items["gmail-ticketmail-refresh"]["command"]
    # once the oauth client is set, gmail's command is the plain gmail-login
    creds.set_secret("google-oauth-client", '{"client":"x"}', backend=fake_keyring)
    items = {it["key"]: it for it in setup.checklist(backend=fake_keyring)}
    assert items["gmail-ticketmail-refresh"]["command"] == "python -I aa.py gmail-login ticketmail"


def test_render_checklist_is_ascii_and_marks_done_vs_todo(tmp_home, fake_keyring):
    creds.set_secret("wiki", "k", backend=fake_keyring)
    text = setup.render_checklist(setup.checklist(backend=fake_keyring))
    assert "[x]" in text and "[ ]" in text
    text.encode("ascii")   # must not raise: safe on a plain Windows console
    assert "[x]" in text and "[ ]" in text
    text.encode("ascii")   # must not raise: safe on a plain Windows console


def test_plan_json_is_agent_drivable(capsys):
    __import__('pytest').importorskip('keyring', reason='needs the [live] extra')
    from twin_agent.onboarding import setup as S
    rc = S.main(["--plan"], backend=_EmptyBackend() if "_EmptyBackend" in dir() else None)
    out = capsys.readouterr().out
    import json
    data = json.loads(out)
    assert "items" in data and isinstance(data["items"], list) and data["items"]
    for it in data["items"]:
        assert {"key", "label", "ok", "required"} <= set(it)
    assert "missing_required" in data
    # safety: the plan never carries a secret value, only presence + next command
    assert "note" in data and "never" in data["note"].lower()
