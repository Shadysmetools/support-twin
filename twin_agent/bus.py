"""In-process event bus (CONTRACT section 10).

One asyncio queue. Publishers call publish() (never blocks). run() dispatches forever;
drain() dispatches until the queue is empty, which is what tests use.
A handler that raises is reported (on_error) and never stops the bus.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

Handler = Callable[[Any], Awaitable[None]]


class Topics:
    EVENT_IN = "event.in"
    REQUEST_NEW = "request.new"
    REQUEST_TRIAGED = "request.triaged"
    REQUEST_UPDATED = "request.updated"
    REQUEST_REOPENED = "request.reopened"      # 2026-11-11: the helpdesk reopens the ticket too
    STATE_CHANGED = "state.changed"
    TIMER_FIRED = "timer.fired"
    PACKET_READY = "packet.ready"
    CHECKS_DONE = "checks.done"
    WORKER_FAILED = "worker.failed"          # 2026-11-12: the Ticket Analyst still routes its ticket
    CARD_OUT = "card.out"
    CALLBACK_IN = "callback.in"
    COMMAND_IN = "command.in"
    REPLY_IN = "reply.in"
    SEND_REQUEST = "send.request"
    SEND_DONE = "send.done"
    KILL_CHANGED = "kill.changed"
    HEALTH = "health"

    @classmethod
    def all(cls) -> set[str]:
        return {v for k, v in vars(cls).items() if k.isupper()}


class Bus:
    def __init__(self, on_error: Callable[[str, BaseException], None] | None = None):
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._queue: asyncio.Queue | None = None
        self._pending: list[tuple[str, Any]] = []  # published before a loop existed
        self._stopped = False
        self.on_error = on_error
        self.published: list[tuple[str, Any]] = []

    def subscribe(self, topic: str, handler: Handler) -> None:
        if topic not in Topics.all():
            raise ValueError(f"unknown topic {topic!r}")
        self._handlers[topic].append(handler)

    def publish(self, topic: str, payload: Any) -> None:
        if topic not in Topics.all():
            raise ValueError(f"unknown topic {topic!r}")
        self.published.append((topic, payload))
        q = self._get_queue()
        if q is None:
            self._pending.append((topic, payload))
        else:
            q.put_nowait((topic, payload))

    def _get_queue(self) -> asyncio.Queue | None:
        if self._queue is None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return None
            self._queue = asyncio.Queue()
            for item in self._pending:
                self._queue.put_nowait(item)
            self._pending.clear()
        return self._queue

    async def _dispatch(self, topic: str, payload: Any) -> None:
        for handler in list(self._handlers.get(topic, ())):
            try:
                await handler(payload)
            except Exception as exc:  # a bad handler must never stop the bus
                log.exception("bus handler failed on %s", topic)
                if self.on_error:
                    self.on_error(topic, exc)

    async def drain(self) -> int:
        """Dispatch until nothing is left (including items handlers publish). Returns the count."""
        q = self._get_queue()
        n = 0
        while q is not None and not q.empty():
            topic, payload = q.get_nowait()
            await self._dispatch(topic, payload)
            n += 1
        return n

    async def run(self) -> None:
        q = self._get_queue()
        assert q is not None
        self._stopped = False
        while not self._stopped:
            try:
                topic, payload = await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            await self._dispatch(topic, payload)

    def stop(self) -> None:
        self._stopped = True
