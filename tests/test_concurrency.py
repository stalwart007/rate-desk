"""Rule 3 under pressure: many approvals of one request at the same instant."""

from __future__ import annotations

import multiprocessing
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from rate_desk.db import Database
from rate_desk.domain import Channel
from rate_desk.errors import AlreadyDecided
from rate_desk.service import RateDesk
from tests.conftest import World

RACERS = 16


def test_racing_threads_approve_a_request_exactly_once(world: World) -> None:
    request = world.raise_ganger()
    start = threading.Barrier(RACERS)

    def approve(i: int) -> str:
        approver = world.priya if i % 2 else world.tom
        start.wait()  # everyone goes at once
        try:
            world.service.approve(request.id, by=approver, via=Channel.API)
        except AlreadyDecided:
            return "refused"
        return "approved"

    with ThreadPoolExecutor(RACERS) as pool:
        results = list(pool.map(approve, range(RACERS)))

    assert results.count("approved") == 1
    assert results.count("refused") == RACERS - 1
    history = world.service.rate_history("viaduct", "ganger")
    assert [rate.source_request_id for rate in history] == [None, request.id]
    approvals = [e for e in world.service.events_after(0) if e.type.value == "request.approved"]
    assert len(approvals) == 1


def _approve_in_another_process(db_path: str, request_id: int, token: str) -> str:
    service = RateDesk(Database(db_path))
    try:
        service.approve(request_id, by=service.authenticate(token), via=Channel.MCP)
    except AlreadyDecided:
        return "refused"
    return "approved"


def test_racing_processes_approve_a_request_exactly_once(world: World, tmp_path: Path) -> None:
    """The API server and a stdio MCP server are separate processes on one database file."""
    request = world.raise_ganger()
    context = multiprocessing.get_context("spawn")
    tokens = [World.TOKENS["priya"], World.TOKENS["tom"]] * 2
    with ProcessPoolExecutor(len(tokens), mp_context=context) as pool:
        results = list(
            pool.map(
                _approve_in_another_process,
                [str(world.db_path)] * len(tokens),
                [request.id] * len(tokens),
                tokens,
            )
        )
    assert sorted(results) == ["approved", "refused", "refused", "refused"]
    assert len(world.service.rate_history("viaduct", "ganger")) == 2
