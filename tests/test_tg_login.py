"""scripts/tg_login: session string goes to the vault only; phone, code, password and session are never printed."""
from types import SimpleNamespace as NS

from twin_agent.channels.tg_client import format_tg_api, parse_tg_api
from scripts import tg_login

PHONE, CODE, PW, SESSION = "+200000000000", "12345", "pw-2fa", "1AbCfakeSessionString=="
API_HASH = "0123456789abcdef0123456789abcdef"


class SessionPasswordNeededError(Exception):
    pass


class FakeClient:
    def __init__(self, needs_2fa=False):
        self.needs_2fa, self.calls, self.session = needs_2fa, [], NS(save=lambda: SESSION)

    async def connect(self):
        self.calls.append("connect")

    async def disconnect(self):
        self.calls.append("disconnect")

    async def send_code_request(self, phone):
        self.calls.append(("code", phone))

    async def sign_in(self, phone=None, code=None, password=None):
        self.calls.append(("sign_in", phone, code, password))
        if self.needs_2fa and password is None:
            raise SessionPasswordNeededError()

    async def get_me(self):
        return NS(id=1001)


def run_login(vault, client, answers, argv=None):
    out, made = [], []
    answers = iter(answers)

    def factory(api_id, api_hash):
        made.append((api_id, api_hash))
        return client
    code = tg_login.main(argv or [], store=lambda n, v: vault.__setitem__(n, v), has=lambda n: n in vault,
                         get=lambda n: vault[n], client_factory=factory, ask_secret=lambda p: next(answers),
                         say=out.append)
    return code, out, made


def test_full_login_with_2fa_stores_only_in_vault(capsys):
    vault, client = {}, FakeClient(needs_2fa=True)
    code, out, made = run_login(vault, client, ["12345678", API_HASH, PHONE, CODE, PW])
    assert code == 0 and vault["tg-user-session"] == SESSION and vault["tg-api"] == f"12345678:{API_HASH}"
    assert made == [(12345678, API_HASH)] and client.calls[-1] == "disconnect"
    text = "\n".join(out) + capsys.readouterr().out
    for secret in (PHONE, CODE, PW, SESSION, API_HASH):
        assert secret not in text
    assert "1001" in text                       # the user id to put in config


def test_existing_session_needs_force():
    vault = {"tg-api": f"1:{API_HASH}", "tg-user-session": "old"}
    code, out, _ = run_login(vault, FakeClient(), [])
    assert code == 1 and vault["tg-user-session"] == "old"
    code, _, _ = run_login(vault, FakeClient(), [PHONE, CODE], argv=["--force"])
    assert code == 0 and vault["tg-user-session"] == SESSION


def test_wrong_code_saves_nothing():
    class Bad(FakeClient):
        async def sign_in(self, **kw):
            raise ValueError(f"PHONE_CODE_INVALID for {PHONE}")
    vault = {"tg-api": f"1:{API_HASH}"}
    code, out, _ = run_login(vault, Bad(), [PHONE, CODE])
    assert code == 3 and "tg-user-session" not in vault and PHONE not in "\n".join(out)


def test_bad_api_values_rejected():
    vault = {}
    code, _, _ = run_login(vault, FakeClient(), ["abc", "nothex"])
    assert code == 2 and vault == {}


def test_tg_api_format():
    assert parse_tg_api(f"42:{API_HASH}") == (42, API_HASH)
    assert parse_tg_api(f"42\n{API_HASH}") == (42, API_HASH)
    assert parse_tg_api('{"api_id": 42, "api_hash": "%s"}' % API_HASH) == (42, API_HASH)
    assert format_tg_api(" 42 ", API_HASH) == f"42:{API_HASH}"


def test_real_client_is_named_for_telegram_devices():
    __import__('pytest').importorskip('telethon', reason='needs the [live] extra')
    """Builds (does not connect) a real Telethon client: Devices must show TwinAgent-Laptop; no session on disk."""
    from twin_agent.channels.tg_client import new_telethon_client
    c = new_telethon_client(1, API_HASH)
    assert c._init_request.device_model == "TwinAgent-Laptop" and c.session.save() == ""
