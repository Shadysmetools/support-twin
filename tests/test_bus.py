"""Event bus (ARCHITECT foundation). No pytest-asyncio: tests use asyncio.run()."""
import asyncio

import pytest

from twin_agent.bus import Bus, Topics


def test_publish_and_drain_in_order():
    async def go():
        bus, seen = Bus(), []

        async def h(p):
            seen.append(p)
            if p == 1:
                bus.publish(Topics.REQUEST_NEW, 2)   # handlers may publish more

        bus.subscribe(Topics.EVENT_IN, h)
        bus.subscribe(Topics.REQUEST_NEW, h)
        bus.publish(Topics.EVENT_IN, 1)
        n = await bus.drain()
        return seen, n

    seen, n = asyncio.run(go())
    assert seen == [1, 2] and n == 2


def test_publish_before_loop_is_kept():
    bus, seen = Bus(), []

    async def h(p):
        seen.append(p)

    bus.subscribe(Topics.HEALTH, h)
    bus.publish(Topics.HEALTH, "early")          # no running loop yet
    asyncio.run(bus.drain())
    assert seen == ["early"]
    assert bus.published == [(Topics.HEALTH, "early")]


def test_bad_handler_does_not_stop_bus():
    errors = []

    async def go():
        bus, seen = Bus(on_error=lambda t, e: errors.append((t, str(e)))), []

        async def bad(p):
            raise RuntimeError("boom")

        async def good(p):
            seen.append(p)

        bus.subscribe(Topics.CARD_OUT, bad)
        bus.subscribe(Topics.CARD_OUT, good)
        bus.publish(Topics.CARD_OUT, "x")
        await bus.drain()
        return seen

    assert asyncio.run(go()) == ["x"]
    assert errors == [(Topics.CARD_OUT, "boom")]


def test_unknown_topic_rejected():
    with pytest.raises(ValueError):
        Bus().publish("no.such.topic", 1)


def test_run_and_stop():
    async def go():
        bus, seen = Bus(), []

        async def h(p):
            seen.append(p)
            bus.stop()

        bus.subscribe(Topics.SEND_DONE, h)
        bus.publish(Topics.SEND_DONE, "done")
        await asyncio.wait_for(bus.run(), timeout=5)
        return seen

    assert asyncio.run(go()) == ["done"]
