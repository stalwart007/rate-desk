"""The wall dashboard's live feed, against a real running server."""

from __future__ import annotations

import json
import signal
import time
from collections.abc import Iterator

import httpx
import pytest

from rate_desk.db import Database
from rate_desk.domain import Channel
from rate_desk.money import Money
from rate_desk.service import RateDesk
from tests.conftest import LiveServer, World

WALL = World.TOKENS["wall"]


def sse(response: httpx.Response) -> Iterator[dict[str, str]]:
    """Parse a text/event-stream into {event, id, data} dicts, skipping comments."""
    fields: dict[str, str] = {}
    for line in response.iter_lines():
        if not line:
            if "event" in fields or "data" in fields:
                yield fields
            fields = {}
        elif not line.startswith(":"):
            key, _, value = line.partition(":")
            fields[key] = value.removeprefix(" ")


def stream(http: httpx.Client, **headers: str) -> httpx.Response:
    return http.send(
        http.build_request(
            "GET", "/api/events/stream", params={"access_token": WALL}, headers=headers
        ),
        stream=True,
    )


@pytest.fixture
def http(live_server: LiveServer) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=live_server.url, timeout=5) as client:
        yield client


def raise_ganger(http: httpx.Client, new: str = "24.00") -> int:
    response = http.post(
        "/api/requests",
        headers={"Authorization": "Bearer t-dave"},
        json={
            "project": "viaduct",
            "trade": "ganger",
            "current_rate": "22.50",
            "requested_rate": new,
            "reason": "Nights",
        },
    )
    response.raise_for_status()
    return int(response.json()["id"])


def test_a_new_screen_gets_a_snapshot_then_each_change_as_it_happens(http: httpx.Client) -> None:
    response = stream(http)
    try:
        assert response.headers["content-type"].startswith("text/event-stream")
        events = sse(response)
        snapshot = next(events)
        assert snapshot["event"] == "snapshot"
        assert json.loads(snapshot["data"])["pending"] == []

        request_id = raise_ganger(http)
        raised = next(events)
        assert raised["event"] == "request.raised"
        assert json.loads(raised["data"])["request"]["id"] == request_id

        http.post(f"/api/requests/{request_id}/approve", headers={"Authorization": "Bearer t-tom"})
        approved = next(events)
        assert approved["event"] == "request.approved"
        body = json.loads(approved["data"])["request"]
        assert body["status"] == "approved"
        assert body["outcome"]["new_rate"] == "24.00"
        assert int(snapshot["id"]) < int(raised["id"]) < int(approved["id"])
    finally:
        response.close()


def test_a_screen_that_reconnects_gets_exactly_what_it_missed(http: httpx.Client) -> None:
    raise_ganger(http, new="23.10")  # so the screen has a real event id to remember
    response = stream(http)
    seen = next(sse(response))["id"]
    response.close()  # the wall's Wi-Fi drops...

    first = raise_ganger(http)  # ...and things happen while it is away
    second = raise_ganger(http, new="25.00")

    response = stream(http, **{"Last-Event-ID": seen})
    try:
        events = sse(response)
        missed = [next(events), next(events)]
        assert [json.loads(e["data"])["request"]["id"] for e in missed] == [first, second]
        assert all(e["event"] == "request.raised" for e in missed)

        third = raise_ganger(http, new="26.00")  # and then it carries on live
        assert json.loads(next(events)["data"])["request"]["id"] == third
    finally:
        response.close()


def test_a_screen_remembering_a_reset_database_starts_from_a_snapshot(http: httpx.Client) -> None:
    response = stream(http, **{"Last-Event-ID": "9999"})
    try:
        assert next(sse(response))["event"] == "snapshot"
    finally:
        response.close()


def test_changes_made_by_another_process_reach_the_screen(
    http: httpx.Client, live_server: LiveServer
) -> None:
    """The stdio MCP server is a separate process writing to the same file.

    A second service on the same database, with no link to the server's
    broadcaster, stands in for it: only the server's polling can find this.
    """
    response = stream(http)
    try:
        events = sse(response)
        next(events)  # snapshot
        elsewhere = RateDesk(Database(live_server.world.db_path))
        dave = elsewhere.authenticate("t-dave")
        request = elsewhere.raise_request(
            by=dave,
            project="viaduct",
            trade="ganger",
            current=Money(2250),
            requested=Money(2400),
            reason="from another process",
        )
        elsewhere.approve(request.id, by=elsewhere.authenticate("t-priya"), via=Channel.MCP)
        assert [next(events)["event"], next(events)["event"]] == [
            "request.raised",
            "request.approved",
        ]
    finally:
        response.close()


def test_the_stream_needs_a_token(http: httpx.Client) -> None:
    assert http.get("/api/events/stream").status_code == 401
    assert http.get("/api/events/stream", params={"access_token": "nope"}).status_code == 401


def test_a_request_overtaken_by_an_approval_shows_as_stale(http: httpx.Client) -> None:
    response = stream(http)
    try:
        events = sse(response)
        next(events)  # snapshot
        first, second = raise_ganger(http), raise_ganger(http, new="23.00")
        next(events), next(events)  # both raised
        http.post(f"/api/requests/{first}/approve", headers={"Authorization": "Bearer t-tom"})
        approved, updated = next(events), next(events)
        assert approved["event"] == "request.approved"
        assert updated["event"] == "request.updated"
        body = json.loads(updated["data"])["request"]
        assert (body["id"], body["stale"], body["rate_in_force"]) == (second, True, "24.00")
    finally:
        response.close()


def test_a_quiet_stream_still_sends_heartbeats_the_page_can_see(
    http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rate_desk.api.HEARTBEAT_SECONDS", 0.2)
    response = stream(http)
    try:
        events = sse(response)
        snapshot = next(events)
        heartbeat = next(events)
        assert heartbeat["event"] == "heartbeat"
        assert "id" not in heartbeat  # must not move the browser's Last-Event-ID
        assert json.loads(heartbeat["data"]) == {"cursor": int(snapshot["id"])}
    finally:
        response.close()


def test_shutdown_ends_open_streams_at_once(http: httpx.Client, live_server: LiveServer) -> None:
    response = stream(http)
    events = sse(response)
    next(events)  # connected
    started = time.monotonic()
    live_server.server.handle_exit(signal.SIGTERM, None)
    assert list(events) == []  # the stream ends cleanly, with nothing half-sent
    live_server.thread.join(timeout=5)
    assert not live_server.thread.is_alive()
    assert time.monotonic() - started < 1.0  # not the 1 s graceful-shutdown timeout
    response.close()


@pytest.mark.parametrize("bad", ["-5", "0", "abc", str(2**63), "99999999999999999999999"])
def test_a_nonsense_last_event_id_gets_a_fresh_snapshot(http: httpx.Client, bad: str) -> None:
    response = stream(http, **{"Last-Event-ID": bad})
    try:
        assert next(sse(response))["event"] == "snapshot"
    finally:
        response.close()


@pytest.mark.parametrize("bad", ["-1", str(2**63), "-100000000000000000000000"])
def test_a_nonsense_since_is_refused_before_the_stream_starts(http: httpx.Client, bad: str) -> None:
    response = http.get("/api/events/stream", params={"access_token": WALL, "since": bad})
    assert response.status_code == 422


def test_the_snapshot_counts_every_decision_even_beyond_those_listed(
    http: httpx.Client, live_server: LiveServer
) -> None:
    service = live_server.world.service
    for i in range(3):
        request_id = raise_ganger(http, new=f"3{i}.00")
        service.decline(request_id, by=live_server.world.tom, reason="no", via=Channel.API)
    response = stream(http)
    try:
        snapshot = json.loads(next(sse(response))["data"])
        assert snapshot["decided_total"] == 3
    finally:
        response.close()
