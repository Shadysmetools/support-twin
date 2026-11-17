"""Shared test fixtures (CONTRACT section 12).

The autouse _no_network fixture makes every non-loopback connection fail, so no test can reach
Telegram, Gmail, ElevenLabs, Odoo or Wiki, even by accident.
"""
from __future__ import annotations

import ipaddress
import shutil
import socket
from pathlib import Path

import pytest
import yaml

from twin_agent.bus import Bus
from twin_agent.testing import fakes

REPO = Path(__file__).parent


class NetworkBlocked(RuntimeError):
    pass


def _is_loopback(host) -> bool:
    if host in ("localhost", "", None):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


ARABIC_TEST_MODULES = {"test_hub_arabic", "test_hub_templates", "test_hub_confirm_slots", "test_hub_followups", "test_hub_lifecycle"}


@pytest.fixture(autouse=True)
def _client_languages(request):
    """Production default is English only, and that is what every test runs with. Only the modules below, which exercise
    the (disabled) Arabic strings, switch Arabic on (or a test marked ``arabic_on``)."""
    from twin_agent.hub import persona
    arabic_on = request.node.get_closest_marker("arabic_on") or request.node.module.__name__.rsplit(".", 1)[-1] in ARABIC_TEST_MODULES
    persona.set_client_languages(["en", "ar"] if arabic_on else ["en"])
    yield
    persona.set_client_languages(["en"])


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    # No test may reach an external model by accident: drop any ambient Groq key so the cross-model refuter
    # (checks/refuter_xmodel.py) stays off unless a test opts in explicitly. Mirrors the network block below.
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_gai = socket.getaddrinfo

    def addr_host(address):
        return address[0] if isinstance(address, tuple) else address

    def guard_connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(addr_host(address)):
            raise NetworkBlocked(f"network call blocked in tests: {address!r}")
        return real_connect(self, address)

    def guard_connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(addr_host(address)):
            raise NetworkBlocked(f"network call blocked in tests: {address!r}")
        return real_connect_ex(self, address)

    def guard_gai(host, *args, **kwargs):
        if not _is_loopback(host):
            raise NetworkBlocked(f"DNS lookup blocked in tests: {host!r}")
        return real_gai(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guard_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guard_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guard_gai)

    # asyncio skips getaddrinfo for IP literals, and on Windows the Proactor loop connects with ConnectEx,
    # not socket.connect. Telethon dials raw IPs this way, so block the event loops' sock_connect as well.
    import asyncio.proactor_events
    import asyncio.selector_events

    for loop_cls in (asyncio.proactor_events.BaseProactorEventLoop, asyncio.selector_events.BaseSelectorEventLoop):
        real = loop_cls.sock_connect

        def make_guard(real_fn):
            async def guard_sock_connect(self, sock, address):
                if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(addr_host(address)):
                    raise NetworkBlocked(f"network call blocked in tests: {address!r}")
                return await real_fn(self, sock, address)
            return guard_sock_connect

        monkeypatch.setattr(loop_cls, "sock_connect", make_guard(real))
    yield


def _test_config() -> dict:
    cfg = yaml.safe_load((REPO / "config.example.yaml").read_text(encoding="utf-8"))
    cfg["people"]["principal_tg_user_id"] = fakes.PRINCIPAL_ID
    cfg["people"]["backup_tg_user_ids"] = [fakes.BACKUP_ID]
    group = cfg["chats"][0]
    group["chat_id"] = fakes.GROUP_ID
    group["topics"] = [{"id": fakes.TOPIC_LOGISTICS, "name": "Logistics", "company": "EG"},
                       {"id": fakes.TOPIC_STORES, "name": "Stores", "company": "EG"}]
    cfg["chats"][1]["chat_id"] = -100600
    return cfg


@pytest.fixture
def tmp_home(tmp_path, monkeypatch) -> Path:
    """A throwaway C:\\TwinAgent. Never touches the real one."""
    monkeypatch.setenv("TWIN_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("TWIN_AGENT_VAULT_DIR", str(tmp_path / "vault"))
    from twin_agent import paths
    paths.ensure_dirs()
    (tmp_path / "config" / "config.yaml").write_text(
        yaml.safe_dump(_test_config(), sort_keys=False, allow_unicode=True), encoding="utf-8")
    shutil.copy(fakes.FIXTURES / "runbook_small.md", tmp_path / "config" / "runbook_small.md")
    return tmp_path


@pytest.fixture
def cfg(tmp_home) -> dict:
    """The test config. Uses config.load_config when CORE has shipped it, else a plain YAML load."""
    path = tmp_home / "config" / "config.yaml"
    try:
        from twin_agent.config import load_config
        return load_config(path)
    except (ImportError, NotImplementedError):
        return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture
def clock() -> fakes.FakeClock:
    return fakes.FakeClock()


@pytest.fixture
def bus() -> Bus:
    return Bus()


@pytest.fixture
def fake_tg() -> fakes.FakeTelegram:
    return fakes.FakeTelegram()


@pytest.fixture
def fake_bot() -> fakes.FakeBot:
    return fakes.FakeBot()


@pytest.fixture
def fake_gmail() -> fakes.FakeGmail:
    return fakes.FakeGmail()


@pytest.fixture
def fake_claude() -> fakes.FakeClaude:
    return fakes.FakeClaude()


@pytest.fixture
def fake_keyring() -> fakes.FakeKeyring:
    return fakes.FakeKeyring()


@pytest.fixture
def make_event():
    return fakes.make_event


@pytest.fixture
def export_path() -> Path:
    return fakes.FIXTURES / "export_small.json"
