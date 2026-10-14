"""The REST API, end to end through FastAPI."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from rate_desk.api import create_app
from rate_desk.config import Settings
from tests.conftest import World


@pytest.fixture
def client(world: World) -> Iterator[TestClient]:
    app = create_app(Settings(db_path=world.db_path), clock=world.clock)
    with TestClient(app) as client:
        yield client


def auth(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {World.TOKENS[name]}"}


GANGER = {
    "project": "viaduct",
    "trade": "ganger",
    "current_rate": "22.50",
    "requested_rate": "24.00",
    "reason": "Night shifts start next week.",
}


def raise_one(client: TestClient, who: str = "dave", **changes: object) -> dict[str, object]:
    response = client.post("/api/requests", json=GANGER | changes, headers=auth(who))
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


def test_every_api_call_needs_a_valid_token(client: TestClient) -> None:
    assert client.get("/api/requests").status_code == 401
    response = client.get("/api/requests", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {"code": "unauthenticated", "message": "unknown token"}
    assert client.get("/api/me", headers=auth("tom")).json() == {
        "id": 4,
        "name": "Tom",
        "can_raise": False,
        "can_approve": True,
    }


def test_raise_list_approve_and_read_back(client: TestClient) -> None:
    raised = raise_one(client)
    assert raised["status"] == "pending"
    assert raised["requested_by"] == {"id": 1, "name": "Dave"}
    assert (raised["change"], raised["change_percent"]) == ("1.50", "6.7")
    assert (raised["currency"], raised["unit"]) == ("GBP", "hour")

    waiting = client.get("/api/requests", headers=auth("tom")).json()
    assert [r["id"] for r in waiting] == [raised["id"]]

    approved = client.post(f"/api/requests/{raised['id']}/approve", headers=auth("tom")).json()
    assert approved["status"] == "approved"
    assert approved["decided_by"]["name"] == "Tom"
    assert approved["decided_via"] == "api"
    assert approved["outcome"]["previous_rate"] == "22.50"
    assert approved["outcome"]["new_rate"] == "24.00"
    assert approved["outcome"]["still_in_force"] is True

    assert client.get("/api/requests", headers=auth("tom")).json() == []
    again = client.get(f"/api/requests/{raised['id']}", headers=auth("wall")).json()
    assert again["outcome"] == approved["outcome"]

    rates = client.get("/api/projects/viaduct/rates", headers=auth("wall")).json()
    assert [(r["trade"], r["rate"], r["set_by_request_id"]) for r in rates["rates"]] == [
        ("ganger", "24.00", raised["id"])
    ]


@pytest.mark.parametrize(
    ("path", "who", "status", "code"),
    [
        ("approve", "dave", 403, "not_permitted"),
        ("approve", "wall", 403, "not_permitted"),
        ("approve", "priya", 403, "four_eyes"),
    ],
)
def test_who_may_approve(client: TestClient, path: str, who: str, status: int, code: str) -> None:
    raised = raise_one(client, who="priya")
    response = client.post(f"/api/requests/{raised['id']}/{path}", headers=auth(who))
    assert (response.status_code, response.json()["code"]) == (status, code)


def test_approving_twice_is_a_conflict(client: TestClient) -> None:
    raised = raise_one(client)
    assert client.post(f"/api/requests/{raised['id']}/approve", headers=auth("tom")).is_success
    response = client.post(f"/api/requests/{raised['id']}/approve", headers=auth("priya"))
    assert response.status_code == 409
    assert response.json()["code"] == "already_decided"


def test_a_stale_request_is_a_conflict(client: TestClient) -> None:
    first = raise_one(client)
    second = raise_one(client, who="sam", requested_rate="23.00")
    client.post(f"/api/requests/{first['id']}/approve", headers=auth("tom"))
    listed = client.get("/api/requests", headers=auth("tom")).json()
    assert [(r["id"], r["stale"], r["rate_in_force"]) for r in listed] == [
        (second["id"], True, "24.00")
    ]
    response = client.post(f"/api/requests/{second['id']}/approve", headers=auth("tom"))
    assert (response.status_code, response.json()["code"]) == (409, "stale")


def test_wrong_rate_now_is_a_conflict_that_says_the_real_rate(client: TestClient) -> None:
    response = client.post(
        "/api/requests", json=GANGER | {"current_rate": "21.00"}, headers=auth("dave")
    )
    assert response.status_code == 409
    assert "£22.50, not £21.00" in response.json()["message"]


def test_decline_needs_a_reason_and_the_asker_sees_it(client: TestClient) -> None:
    raised = raise_one(client)
    path = f"/api/requests/{raised['id']}/decline"
    assert client.post(path, json={"reason": ""}, headers=auth("tom")).status_code == 422
    assert client.post(path, json={}, headers=auth("tom")).status_code == 422
    declined = client.post(path, json={"reason": "Client said no."}, headers=auth("tom"))
    assert declined.json()["status"] == "declined"

    mine = client.get("/api/requests?status=all&mine=true", headers=auth("dave")).json()
    assert [(r["id"], r["decline_reason"]) for r in mine] == [(raised["id"], "Client said no.")]
    assert client.get("/api/requests?status=all&mine=true", headers=auth("sam")).json() == []


@pytest.mark.parametrize(
    "bad",
    [
        {"requested_rate": 24.0},  # a JSON number: refused, not coerced
        {"requested_rate": 24},
        {"requested_rate": "24.005"},
        {"requested_rate": "-24.00"},
        {"requested_rate": "1e3"},
        {"current_rate": 22.5},
        {"reason": ""},
        {"surprise": "field"},
    ],
)
def test_money_must_arrive_as_exact_decimal_strings(
    client: TestClient, bad: dict[str, object]
) -> None:
    response = client.post("/api/requests", json=GANGER | bad, headers=auth("dave"))
    assert response.status_code == 422, response.text


def test_rates_can_be_read_as_of_any_moment(client: TestClient, world: World) -> None:
    before = world.clock.now
    raised = raise_one(client)
    world.clock.advance(hours=2)
    client.post(f"/api/requests/{raised['id']}/approve", headers=auth("tom"))

    def rate(as_of: str | None = None) -> str:
        params = {"as_of": as_of} if as_of else {}
        body = client.get("/api/projects/viaduct/rates", params=params, headers=auth("wall"))
        return str(body.json()["rates"][0]["rate"])

    assert rate(before.isoformat()) == "22.50"
    assert rate() == "24.00"
    naive = client.get(
        "/api/projects/viaduct/rates", params={"as_of": "2026-10-14T09:00"}, headers=auth("wall")
    )
    assert naive.status_code == 422


def test_history_and_explanation(client: TestClient) -> None:
    raised = raise_one(client)
    client.post(f"/api/requests/{raised['id']}/approve", headers=auth("tom"))
    history = client.get("/api/projects/viaduct/trades/ganger/history", headers=auth("wall"))
    assert [h["rate"] for h in history.json()] == ["22.50", "24.00"]
    text = client.get(f"/api/requests/{raised['id']}/explanation", headers=auth("wall")).text
    assert "from £22.50 to £24.00, up £1.50 an hour (+6.7%)" in text


def test_unknown_things_are_404(client: TestClient) -> None:
    assert client.get("/api/requests/999", headers=auth("tom")).status_code == 404
    assert client.get("/api/projects/nope/rates", headers=auth("tom")).status_code == 404
    response = client.post("/api/requests", json=GANGER | {"trade": "x"}, headers=auth("dave"))
    assert response.status_code == 404


def test_dashboard_page_and_health(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "EventSource" in page.text
    assert client.get("/healthz").json()["ok"] is True


def test_invalid_input_uses_the_same_error_shape_as_every_other_refusal(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/requests", json=GANGER | {"requested_rate": 24.0}, headers=auth("dave")
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "invalid"
    assert body["message"].startswith("requested_rate: ")


@pytest.mark.parametrize("request_id", ["0", "-1", str(2**63), "99999999999999999999"])
def test_impossible_request_ids_are_invalid_not_server_errors(
    client: TestClient, request_id: str
) -> None:
    assert client.get(f"/api/requests/{request_id}", headers=auth("tom")).status_code == 422
    response = client.post(f"/api/requests/{request_id}/approve", headers=auth("tom"))
    assert response.status_code == 422
