"""Shared secrets scanner (ARCHITECT foundation). All sample secrets below are fake."""
from twin_agent import redact


def kinds(text, **kw):
    return [f.kind for f in redact.scan(text, **kw)]


def test_portal_token_and_strip():
    url = "https://helpdesk.example.com/my/ticket/1001/0a1b2c3d-4e5f-6789-abcd-ef0123456789"
    assert "portal_token" in kinds(url)
    assert redact.strip_url_token(url) == "https://helpdesk.example.com/my/ticket/1001"
    assert redact.strip_url_token("https://x.io/my/ticket/5?access_token=abcdef123456&x=1") == \
        "https://x.io/my/ticket/5?x=1"


def test_common_secret_shapes():
    assert "bearer" in kinds("Authorization: Bearer abcdefghijklmnop1234567890")
    assert "kc_key" in kinds("key kc_live_abcdefghijklmnop1234")
    assert "token_param" in kinds("https://x.io/web/login?token=zzzzzz999")
    assert "password_kv" in kinds("password: Hunter2024")
    assert "signup_link" in kinds("https://x.io/web/signup?db=x&token=AbC123xyz")
    assert "api_key" in kinds("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")


def test_phone_numbers_but_not_odoo_refs():
    assert "phone" in kinds("call me on 01012345678")
    assert "phone" in kinds("+20 100 123 4567")
    for ref in ("WAVE/01234", "IN/10001/26", "SO10001", "DLV/10001/26", "stock.move 1000001", "2026-11-02 14:40"):
        assert redact.scan(ref) == [], ref


def test_redact_replaces_and_has_secrets():
    text = "token=supersecret1 and mail a@b.example.org"
    out = redact.redact(text)
    assert "supersecret1" not in out and "[REDACTED:token_param]" in out and "[REDACTED:email]" in out
    assert redact.has_secrets(text)
    assert not redact.has_secrets("mail a@b.example.org only")          # email is personal data, not a secret
    assert not redact.has_secrets("Checking it, 10 min.")


def test_vault_value_match_by_hash():
    secret = "plainlookingvalue"
    h = redact.value_hash(secret)
    assert "vault_value" in kinds(f"oops {secret} leaked", vault_hashes=[h])
    assert secret not in redact.redact(f"oops {secret}", vault_hashes=[h])


# ---------------------------------------------------------------- review fix: hex keys

def test_hex_api_keys_are_secrets():
    """Odoo API keys (40 hex) and Telegram api_hash (32 hex) have entropy <= 4.0, so only a pattern catches them."""
    from twin_agent import redact as R
    assert R.has_secrets("key 3f2a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a")
    assert R.has_secrets("api_hash 0123456789abcdef0123456789abcdef")
    assert "3f2a9b8c" not in R.redact("key 3f2a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a")


def test_sha256_hex_and_plain_numbers_are_not_hex_keys():
    from twin_agent import redact as R
    assert not any(f.kind == "hex_key" for f in R.scan("text_sha256 " + "ab12" * 16))
    assert not R.scan("order SO80924 on 20261102, wave WAVE/06733")


def test_outbound_guard_blocks_leaked_key_shapes():
    from twin_agent.checks import guard_text as T
    for s in ("sk-ant-oat01-" + "Ab3dE9" * 10, "123456789:AAHk3Jd9s0QwErTyUiOpAsDfGhJkLzXcVbN",
              "1//0gAbCdEfGhIjK-LmNoPqRsTuVwXyZ0123456789abcdef", "3f2a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a"):
        assert T.secrets("please use " + s + " now"), s[:12]
