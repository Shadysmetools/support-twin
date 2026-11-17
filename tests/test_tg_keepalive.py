"""Telegram keepalive (Principal, 2026-11-10): reconnect by itself, one card down, one card back, sweep after."""
import asyncio
from datetime import datetime, timedelta, timezone

from twin_agent import flow_timers, listener


class FakeClient:
    def __init__(self, fails_before_ok: int):
        self.connected, self.fails, self.connects, self.caught_up = False, fails_before_ok, 0, 0

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connects += 1
        if self.fails > 0:
            self.fails -= 1
            raise ConnectionError("Connection to Telegram failed 5 time(s)")
        self.connected = True

    async def catch_up(self):
        self.caught_up += 1


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 10, 12, 29, tzinfo=timezone.utc)

    def now(self):
        self.t += timedelta(minutes=1)
        return self.t


class Bus:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload):
        self.sent.append(payload)


class DB:
    def __init__(self):
        self.events = []

    def log_event(self, kind, **kw):
        self.events.append(kind)


class Flow:
    def __init__(self):
        self.clock, self.bus, self.db = Clock(), Bus(), DB()


def _run(client, flow, monkeypatch, rounds):
    swept = []

    async def fake_sweep(f):
        swept.append(f)
    monkeypatch.setattr(flow_timers, "sweep", fake_sweep)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(listener._tg_keepalive(client, flow, stop, check_s=0.001, max_wait_s=0.002))
        for _ in range(rounds):
            await asyncio.sleep(0.01)
        stop.set()
        await task
    asyncio.run(go())
    return swept


def test_reconnects_after_drop_with_one_card_each_way_and_a_sweep(monkeypatch):
    client, flow = FakeClient(fails_before_ok=3), Flow()
    swept = _run(client, flow, monkeypatch, rounds=20)
    assert client.connected and client.connects == 4 and client.caught_up == 1
    oks = [p["ok"] for p in flow.bus.sent if p["component"] == "telegram"]
    assert oks == [False, True]                      # one "problem", one "back to normal", no spam while down
    assert "telegram_reconnected" in flow.db.events and len(swept) == 1


def test_connected_client_is_left_alone(monkeypatch):
    client, flow = FakeClient(fails_before_ok=0), Flow()
    client.connected = True
    swept = _run(client, flow, monkeypatch, rounds=5)
    assert client.connects == 0 and flow.bus.sent == [] and swept == []


def test_intake_is_started_once_the_connection_is_back(monkeypatch):
    client, flow, started = FakeClient(fails_before_ok=2), Flow(), []

    async def start_intake():
        if not started:
            started.append(1)

    async def fake_sweep(f):
        pass
    monkeypatch.setattr(flow_timers, "sweep", fake_sweep)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(listener._tg_keepalive(client, flow, stop, check_s=0.001, max_wait_s=0.002,
                                                          start_intake=start_intake))
        await asyncio.sleep(0.1)
        stop.set()
        await task
    asyncio.run(go())
    assert client.connected and started == [1]
