from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
import uvicorn

from rate_desk.api import create_app
from rate_desk.cli import Server
from rate_desk.config import Settings
from rate_desk.db import Database
from rate_desk.domain import RateChangeRequest, User
from rate_desk.money import Money
from rate_desk.service import RateDesk


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Clock:
    """A clock the test moves by hand, so times in assertions are exact."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


@dataclass
class World:
    """A small, known setup: one project, two trades, four people."""

    service: RateDesk
    clock: Clock
    db_path: Path
    dave: User  # raises
    sam: User  # raises
    priya: User  # raises and approves
    tom: User  # approves
    wall: User  # does neither
    tester: User  # demo only: acts as the others

    TOKENS: ClassVar[dict[str, str]] = {
        "dave": "t-dave",
        "sam": "t-sam",
        "priya": "t-priya",
        "tom": "t-tom",
        "wall": "t-wall",
        "tester": "t-tester",
    }

    def raise_ganger(
        self, by: User | None = None, *, now: str | None = "22.50", new: str = "24.00"
    ) -> RateChangeRequest:
        return self.service.raise_request(
            by=by or self.dave,
            project="viaduct",
            trade="ganger",
            current=None if now is None else Money.parse(now),
            requested=Money.parse(new),
            reason="Night shifts start next week.",
        )


def build_world(db_path: Path, clock: Clock | None = None) -> World:
    clock = clock or Clock()
    db = Database(db_path)
    db.migrate()
    service = RateDesk(db, clock=clock)
    service.add_project("viaduct", "Riverside Viaduct")
    service.add_trade("ganger", "Ganger")
    service.add_trade("steel-fixer", "Steel Fixer")
    service.load_opening_rate("viaduct", "ganger", Money.parse("22.50"))
    clock.advance(hours=1)
    users = {
        name: service.add_user(
            name.title(),
            can_raise=name in {"dave", "sam", "priya"},
            can_approve=name in {"priya", "tom"},
            can_act_as_others=name == "tester",
            token=token,
        )[0]
        for name, token in World.TOKENS.items()
    }
    return World(service, clock, db_path, **users)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def world(tmp_path: Path, clock: Clock) -> World:
    return build_world(tmp_path / "rate-desk.db", clock)


@dataclass
class LiveServer:
    url: str
    world: World
    server: Server
    thread: threading.Thread


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[LiveServer]:
    """The real app, served by uvicorn on a free port, for streaming and MCP-over-HTTP tests."""
    world = build_world(tmp_path / "live.db")
    app = create_app(Settings(db_path=world.db_path), poll_interval=0.1)
    server = Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="warning", timeout_graceful_shutdown=1
        ),
        app.state.broadcaster,
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    # The app has its own service; give tests the one wired to the live broadcaster.
    world.service = app.state.service
    yield LiveServer(f"http://127.0.0.1:{port}", world, server, thread)
    server.should_exit = True
    thread.join(timeout=10)
