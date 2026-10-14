"""The demo tester: one token that can act as each demo person, from the dashboard.

The tester is a puppeteer, not a superuser. Acting as Dave, it has exactly
Dave's rights; four-eyes is checked between the people it acts as; and every
action records the tester as well as the person.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rate_desk.api import create_app
from rate_desk.config import Settings
from rate_desk.db import SCHEMA_VERSION, Database
from rate_desk.domain import Channel, Status
from rate_desk.errors import FourEyesViolation, InvalidRequest, NotFound, NotPermitted
from rate_desk.explain import explain
from rate_desk.money import Money
from rate_desk.seed import seed_demo
from rate_desk.service import RateDesk
from tests.conftest import World

# ------------------------------------------------------------------ service


def test_acting_as_someone_uses_their_rights_and_records_the_tester(world: World) -> None:
    as_dave = world.service.act_as(world.tester, world.dave.id)
    raised = world.service.raise_request(
        by=as_dave.user,
        proxy=as_dave.proxy,
        project="viaduct",
        trade="ganger",
        current=Money(2250),
        requested=Money(2400),
        reason="Testing",
    )
    assert raised.requested_by == world.dave
    assert raised.raised_by_proxy is not None
    assert raised.raised_by_proxy.id == world.tester.id

    as_tom = world.service.act_as(world.tester, world.tom.id)
    approved = world.service.approve(raised.id, by=as_tom.user, via=Channel.API, proxy=as_tom.proxy)
    assert approved.decided_by == world.tom
    assert approved.decided_by_proxy is not None
    assert approved.decided_by_proxy.id == world.tester.id
    assert "approved by Tom (Tester, acting as Tom)" in explain(approved)
    assert "Who asked: Dave (Tester, acting as Dave)" in explain(approved)


def test_acting_as_someone_gives_only_their_rights(world: World) -> None:
    request = world.raise_ganger()
    as_dave = world.service.act_as(world.tester, world.dave.id)
    with pytest.raises(NotPermitted, match="Dave is not allowed to approve"):
        world.service.approve(request.id, by=as_dave.user, via=Channel.API, proxy=as_dave.proxy)


def test_four_eyes_still_holds_between_the_people_acted_as(world: World) -> None:
    as_priya = world.service.act_as(world.tester, world.priya.id)
    own = world.raise_ganger(by=as_priya.user)
    with pytest.raises(FourEyesViolation):
        world.service.approve(own.id, by=as_priya.user, via=Channel.API, proxy=as_priya.proxy)


def test_only_the_tester_can_act_as_anyone(world: World) -> None:
    with pytest.raises(NotPermitted, match="cannot act as anyone else"):
        world.service.act_as(world.tom, world.dave.id)
    with pytest.raises(NotPermitted):
        world.service.people(caller=world.tom)
    # The service refuses a forged proxy too, not only the API.
    with pytest.raises(NotPermitted):
        world.service.approve(
            world.raise_ganger().id, by=world.tom, via=Channel.API, proxy=world.priya
        )


def test_the_tester_cannot_act_as_itself_or_nobody(world: World) -> None:
    with pytest.raises(InvalidRequest):
        world.service.act_as(world.tester, world.tester.id)
    with pytest.raises(NotFound):
        world.service.act_as(world.tester, 999)
    assert world.tester.id not in [p.id for p in world.service.people(caller=world.tester)]


def test_the_tester_has_no_rights_of_its_own(world: World) -> None:
    with pytest.raises(NotPermitted):
        world.raise_ganger(by=world.tester)
    with pytest.raises(NotPermitted):
        world.service.approve(world.raise_ganger().id, by=world.tester, via=Channel.API)


# ---------------------------------------------------------------------- API


@pytest.fixture
def demo_client(world: World) -> Iterator[TestClient]:
    app = create_app(Settings(db_path=world.db_path, demo=True), clock=world.clock)
    with TestClient(app) as client:
        yield client


def as_(world: World, person: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer t-tester",
        "X-Act-As": str(getattr(world, person).id),
    }


GANGER = {
    "project": "viaduct",
    "trade": "ganger",
    "current_rate": "22.50",
    "requested_rate": "24.00",
    "reason": "Night shifts",
}


def test_one_token_can_play_every_role_on_a_demo_server(
    demo_client: TestClient, world: World
) -> None:
    raised = demo_client.post("/api/requests", json=GANGER, headers=as_(world, "dave")).json()
    assert raised["requested_by"]["name"] == "Dave"
    assert raised["raised_by_proxy"]["name"] == "Tester"

    path = f"/api/requests/{raised['id']}"
    refused = demo_client.post(f"{path}/approve", headers=as_(world, "dave"))
    assert (refused.status_code, refused.json()["code"]) == (403, "not_permitted")

    approved = demo_client.post(f"{path}/approve", headers=as_(world, "tom")).json()
    assert approved["decided_by"]["name"] == "Tom"
    assert approved["decided_by_proxy"]["name"] == "Tester"
    assert approved["outcome"]["new_rate"] == "24.00"


def test_the_tester_sees_who_it_can_be(demo_client: TestClient) -> None:
    people = demo_client.get("/api/people", headers={"Authorization": "Bearer t-tester"}).json()
    assert [(p["name"], p["can_raise"], p["can_approve"]) for p in people] == [
        ("Dave", True, False),
        ("Sam", True, False),
        ("Priya", True, True),
        ("Tom", False, True),
        ("Wall", False, False),
    ]
    assert (
        demo_client.get("/api/people", headers={"Authorization": "Bearer t-tom"}).status_code == 403
    )


def test_ordinary_tokens_cannot_act_as_anyone(demo_client: TestClient, world: World) -> None:
    headers = {"Authorization": "Bearer t-tom", "X-Act-As": str(world.dave.id)}
    response = demo_client.post("/api/requests", json=GANGER, headers=headers)
    assert (response.status_code, response.json()["code"]) == (403, "not_permitted")


def test_acting_as_is_refused_on_a_real_server(world: World) -> None:
    app = create_app(Settings(db_path=world.db_path, demo=False), clock=world.clock)
    with TestClient(app) as client:
        response = client.post("/api/requests", json=GANGER, headers=as_(world, "dave"))
    assert response.status_code == 403
    assert "only possible on a demo server" in response.json()["message"]


def test_without_the_header_the_tester_is_just_itself(demo_client: TestClient) -> None:
    headers = {"Authorization": "Bearer t-tester"}
    me = demo_client.get("/api/me", headers=headers).json()
    assert me["can_act_as_others"] is True
    assert demo_client.post("/api/requests", json=GANGER, headers=headers).status_code == 403


def test_the_dashboard_has_the_tester_controls(demo_client: TestClient) -> None:
    page = demo_client.get("/").text
    assert "X-Act-As" in page
    assert 'id="tester"' in page


# ---------------------------------------------------------------- migration


def test_a_version_1_database_upgrades_in_place_keeping_its_data(tmp_path: Path) -> None:
    """The hosted demo was created at version 1; it must upgrade on deploy, not be rebuilt."""
    db = Database(tmp_path / "old.db")
    db.migrate(target=1)
    conn = db.connect()
    try:
        conn.execute("INSERT INTO projects VALUES ('p', 'Project')")
        conn.execute("INSERT INTO trades VALUES ('t', 'Trade')")
        conn.execute(
            "INSERT INTO users (name, token_sha256, can_raise, can_approve)"
            " VALUES ('Old', 'x', 1, 0)"
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        conn.close()

    db.migrate()

    service = RateDesk(db)
    assert service.get_user(1).name == "Old"
    assert service.get_user(1).can_act_as_others is False
    conn = db.connect()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2
        conn.execute(
            "INSERT INTO rate_change_requests (project_code, trade_code, requested_pence,"
            " reason, requested_by, requested_at) VALUES ('p', 't', 100, 'r', 1, 'now')"
        )
        # The re-created trigger covers the new column: who entered a request is fixed.
        with pytest.raises(sqlite3.IntegrityError, match="cannot be edited"):
            conn.execute("UPDATE rate_change_requests SET raised_by_proxy = 1")
    finally:
        conn.close()


def test_an_existing_demo_gets_its_tester_on_upgrade(tmp_path: Path) -> None:
    service = RateDesk(Database(tmp_path / "demo.db"))
    Database(tmp_path / "demo.db").migrate()
    service.add_user("Someone", can_raise=True, can_approve=False)
    assert seed_demo(service, only_if_empty=True) == {"Tester": "rd_demo_tester"}
    assert service.authenticate("rd_demo_tester").can_act_as_others
    assert seed_demo(service, only_if_empty=True) == {}  # only once
    assert service.list_requests(status=Status.PENDING) == []  # nothing else was seeded
