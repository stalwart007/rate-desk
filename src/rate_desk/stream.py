"""Live updates for the wall dashboard.

The ``events`` table is the source of truth. It is written in the same
transaction as every change, so it never holds an event for something that
rolled back and never misses one that committed. This module only moves
those rows to the screens watching:

* One :class:`Broadcaster` per process tails the table and hands each new
  event to every subscriber. However many screens are watching, there is
  one reader of the database, not one per connection.
* It wakes instantly when this process commits a change (``nudge``), and
  otherwise every ``poll_interval`` seconds. The poll catches changes made by
  *another* process on the same database, such as the stdio MCP server.
* A screen that reconnects says which event it saw last (``Last-Event-ID``);
  the stream replays what it missed from the table. A screen that falls too
  far behind is disconnected, and reconnecting does the same catch-up. Nothing
  is ever silently dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from rate_desk.domain import Event
from rate_desk.service import RateDesk

log = logging.getLogger(__name__)


class Subscription:
    """One connected screen's queue of events."""

    def __init__(self, broadcaster: Broadcaster, size: int) -> None:
        self._broadcaster = broadcaster
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(size)
        self.lagged = False

    def offer(self, event: Event | None) -> None:
        if self.lagged:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # The screen is not keeping up. Rather than buffer without limit,
            # end its stream; it reconnects and replays from the database.
            self.lagged = True
            log.warning("a live subscriber fell behind; disconnecting it to catch up on reconnect")

    async def next(self) -> Event | None:
        """The next event, or None when this stream should end (shutdown or lagging)."""
        if self.lagged:
            return None
        return await self._queue.get()

    def close(self) -> None:
        self._broadcaster._subscribers.discard(self)


class Broadcaster:
    def __init__(
        self, service: RateDesk, *, poll_interval: float = 1.0, queue_size: int = 256
    ) -> None:
        self._service = service
        self._poll_interval = poll_interval
        self._queue_size = queue_size
        self._subscribers: set[Subscription] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        self._cursor = 0
        self._closed = False

    def subscribe(self) -> Subscription:
        sub = Subscription(self, self._queue_size)
        if self._closed:
            sub.offer(None)  # shutting down: this stream ends at once
        self._subscribers.add(sub)
        return sub

    def nudge(self) -> None:
        """Wake the tailer now. Safe from any thread; the service runs in worker threads."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            # RuntimeError: the loop closed between the check and the call.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._wake.set)

    def close(self) -> None:
        """End every open stream, now. Safe from any thread, including a signal handler.

        Called as soon as the server is asked to stop. Otherwise the server waits
        for open dashboard streams to finish, which they never do by themselves.
        Screens reconnect to the next server and replay what they missed.
        """

        def end_all() -> None:
            self._closed = True
            for sub in list(self._subscribers):
                sub.offer(None)

        loop = self._loop
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(end_all)

    async def start(self) -> None:
        """Note where the table ends now. Call before the server accepts connections, so
        nothing committed between a screen's snapshot and the tailer's first read is skipped."""
        self._loop = asyncio.get_running_loop()
        self._cursor = await asyncio.to_thread(self._service.latest_event_id)

    async def run(self) -> None:
        """Tail the events table until cancelled. Started once, after :meth:`start`."""
        try:
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self._poll_interval)
                self._wake.clear()
                try:
                    await self._pump()
                except Exception:
                    # A failed read (disk full, locked too long) must not kill
                    # live updates for good. Log it and try again next tick.
                    log.exception("could not read new events; retrying")
        finally:
            self._loop = None
            for sub in list(self._subscribers):
                sub.offer(None)

    async def _pump(self) -> None:
        while True:
            events = await asyncio.to_thread(self._service.events_after, self._cursor)
            if not events:
                return
            for event in events:
                for sub in list(self._subscribers):
                    sub.offer(event)
            self._cursor = events[-1].id
