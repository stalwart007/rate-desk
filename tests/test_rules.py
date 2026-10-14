"""The rules in the brief, one test (or a few) per rule, through the service layer."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from rate_desk.domain import Channel, EventType, Status
from rate_desk.errors import (
    AlreadyDecided,
    FourEyesViolation,
    InvalidRequest,
    NotFound,
    NotPermitted,
    RateMismatch,
    StaleRequest,
)
from rate_desk.money import Money
from tests.conftest import World


def in_force(world: World) -> dict[str, Money]:
    _, _, rates = world.service.rates_in_force("viaduct")
    return {rate.trade.code: rate.amount for rate in rates}


# ---------------------------------------------------------------- raising


def test_raising_records_who_asked_and_what_they_asked_for(world: World) -> None:
    request = world.raise_ganger()
    assert request.status is Status.PENDING
    assert request.requested_by == world.dave
    assert (request.current, request.requested) == (Money(2250), Money(2400))
    assert request.change == Money(150)
    assert request.reason == "Night shifts start next week."
    assert request.requested_at == world.clock.now


def test_the_rate_now_must_be_the_rate_actually_in_force(world: World) -> None:
    with pytest.raises(RateMismatch, match=r"£22\.50, not £23\.00"):
        world.raise_ganger(now="23.00")
    with pytest.raises(RateMismatch, match="not no rate yet"):
        world.raise_ganger(now=None)


def test_a_first_rate_is_asked_for_with_no_rate_now(world: World) -> None:
    request = world.service.raise_request(
        by=world.dave,
        project="viaduct",
        trade="steel-fixer",
        current=None,
        requested=Money.parse("25.50"),
        reason="Abutments start next week.",
    )
    assert request.current is None
    assert request.change is None
    with pytest.raises(RateMismatch):
        world.service.raise_request(
            by=world.dave,
            project="viaduct",
            trade="steel-fixer",
            current=Money.parse("20.00"),
            requested=Money.parse("25.50"),
            reason="x",
        )


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"requested": Money(2250)}, InvalidRequest),  # no change at all
        ({"requested": Money(0)}, InvalidRequest),
        ({"reason": "   "}, InvalidRequest),
        ({"reason": "x" * 2001}, InvalidRequest),
        ({"project": "nowhere"}, NotFound),
        ({"trade": "astronaut"}, NotFound),
    ],
)
def test_bad_requests_are_refused(world: World, kwargs: dict[str, object], error: type) -> None:
    args: dict[str, object] = {
        "project": "viaduct",
        "trade": "ganger",
        "current": Money(2250),
        "requested": Money(2400),
        "reason": "ok",
    } | kwargs
    with pytest.raises(error):
        world.service.raise_request(by=world.dave, **args)  # type: ignore[arg-type]


def test_only_people_allowed_to_raise_can_raise(world: World) -> None:
    for user in (world.tom, world.wall):
        with pytest.raises(NotPermitted):
            world.raise_ganger(by=user)


# ------------------------------------------- Rule 1: nothing changes until approval


def test_rule_1_a_waiting_request_does_not_alter_a_rate(world: World) -> None:
    before = in_force(world)
    history_before = world.service.rate_history("viaduct", "ganger")

    world.raise_ganger()
    world.clock.advance(hours=1)

    assert in_force(world) == before == {"ganger": Money(2250)}
    assert world.service.rate_history("viaduct", "ganger") == history_before


def test_rule_1_a_declined_request_does_not_alter_a_rate_either(world: World) -> None:
    request = world.raise_ganger()
    world.service.decline(request.id, by=world.tom, reason="Not this month.", via=Channel.API)
    assert in_force(world) == {"ganger": Money(2250)}
    assert len(world.service.rate_history("viaduct", "ganger")) == 1


def test_the_change_takes_effect_at_approval_and_not_before(world: World) -> None:
    request = world.raise_ganger()
    raised_at = world.clock.now
    approved_at = world.clock.advance(hours=3)
    world.service.approve(request.id, by=world.priya, via=Channel.API)

    def rate_at(moment: datetime) -> Money:
        _, _, rates = world.service.rates_in_force("viaduct", as_of=moment)
        return rates[0].amount

    assert rate_at(raised_at) == Money(2250)
    assert rate_at(approved_at - timedelta(microseconds=1)) == Money(2250)
    assert rate_at(approved_at) == Money(2400)
    assert in_force(world) == {"ganger": Money(2400)}


# ---------------------------------------- Rule 2: an approval records what it became


def test_rule_2_an_approval_records_what_it_did(world: World) -> None:
    request = world.raise_ganger()
    world.clock.advance(minutes=5)
    approved = world.service.approve(request.id, by=world.priya, via=Channel.MCP)

    assert approved.status is Status.APPROVED
    assert approved.decided_by == world.priya
    assert approved.decided_at == world.clock.now
    assert approved.decided_via is Channel.MCP
    outcome = approved.outcome
    assert outcome is not None
    assert outcome.previous_amount == Money(2250)
    assert outcome.resulting_amount == Money(2400)
    assert outcome.effective_from == world.clock.now
    assert outcome.still_in_force

    history = world.service.rate_history("viaduct", "ganger")
    assert [r.id for r in history] == [outcome.previous_rate_id, outcome.resulting_rate_id]
    assert history[1].source_request_id == request.id


def test_rule_2_holds_after_the_rate_has_moved_on(world: World) -> None:
    first = world.service.approve(world.raise_ganger().id, by=world.priya, via=Channel.API)
    replaced_at = world.clock.advance(days=7)
    second = world.raise_ganger(by=world.sam, now="24.00", new="25.00")
    world.service.approve(second.id, by=world.tom, via=Channel.API)

    # Asking later what the first approval did still gives the same answer,
    # plus what happened to its rate since.
    later = world.service.get_request(first.id)
    assert later.outcome is not None
    assert later.outcome.previous_amount == Money(2250)
    assert later.outcome.resulting_amount == Money(2400)
    assert not later.outcome.still_in_force
    assert later.outcome.superseded_at == replaced_at
    assert later.outcome.superseded_by_request_id == second.id


# ---------------------------------------- Rule 3: never approved twice


def test_rule_3_the_same_request_cannot_be_approved_twice(world: World) -> None:
    request = world.raise_ganger()
    world.service.approve(request.id, by=world.priya, via=Channel.API)
    for approver in (world.priya, world.tom):
        with pytest.raises(AlreadyDecided, match="already approved by Priya"):
            world.service.approve(request.id, by=approver, via=Channel.API)
    assert len(world.service.rate_history("viaduct", "ganger")) == 2


def test_a_decision_is_final_either_way(world: World) -> None:
    declined = world.raise_ganger()
    world.service.decline(declined.id, by=world.tom, reason="No.", via=Channel.API)
    with pytest.raises(AlreadyDecided):
        world.service.approve(declined.id, by=world.priya, via=Channel.API)

    approved = world.raise_ganger()
    world.service.approve(approved.id, by=world.tom, via=Channel.API)
    with pytest.raises(AlreadyDecided):
        world.service.decline(
            approved.id, by=world.priya, reason="Changed my mind", via=Channel.API
        )


# ---------------------------------------- Four eyes: somebody else approves


def test_nobody_decides_their_own_request(world: World) -> None:
    request = world.raise_ganger(by=world.priya)  # Priya may raise and approve, but not both
    with pytest.raises(FourEyesViolation, match="Somebody else"):
        world.service.approve(request.id, by=world.priya, via=Channel.API)
    with pytest.raises(FourEyesViolation):
        world.service.decline(request.id, by=world.priya, reason="Withdrawn", via=Channel.API)
    assert world.service.approve(request.id, by=world.tom, via=Channel.API).decided_by == world.tom


def test_only_approvers_approve_or_decline(world: World) -> None:
    request = world.raise_ganger()
    for user in (world.sam, world.wall):
        with pytest.raises(NotPermitted):
            world.service.approve(request.id, by=user, via=Channel.API)
        with pytest.raises(NotPermitted):
            world.service.decline(request.id, by=user, reason="no", via=Channel.API)


# ---------------------------------------- Stale requests


def test_a_request_overtaken_by_another_approval_cannot_be_approved(world: World) -> None:
    a = world.raise_ganger(new="24.00")
    b = world.raise_ganger(by=world.sam, new="23.00")
    world.service.approve(a.id, by=world.priya, via=Channel.API)

    b_now = world.service.get_request(b.id)
    assert b_now.is_stale
    assert b_now.rate_in_force == Money(2400)
    with pytest.raises(StaleRequest, match=r"raised against £22\.50.*is now £24\.00"):
        world.service.approve(b.id, by=world.tom, via=Channel.API)
    assert in_force(world) == {"ganger": Money(2400)}
    # It can still be declined, so the requester hears about it.
    declined = world.service.decline(b.id, by=world.tom, reason="Overtaken", via=Channel.API)
    assert declined.status is Status.DECLINED


# ---------------------------------------- Declining


def test_a_decline_carries_a_reason_the_asker_can_see(world: World) -> None:
    request = world.raise_ganger()
    with pytest.raises(InvalidRequest):
        world.service.decline(request.id, by=world.tom, reason="  ", via=Channel.API)
    world.service.decline(
        request.id, by=world.tom, reason="Client hasn't agreed it yet.", via=Channel.API
    )
    mine = world.service.list_requests(status=None, requested_by=world.dave.id)
    assert [(r.id, r.decline_reason) for r in mine] == [
        (request.id, "Client hasn't agreed it yet.")
    ]


def test_listing_waiting_requests_oldest_first(world: World) -> None:
    first = world.raise_ganger()
    world.clock.advance(minutes=1)
    second = world.raise_ganger(by=world.sam, new="23.00")
    decided = world.raise_ganger(new="26.00")
    world.service.decline(decided.id, by=world.tom, reason="no", via=Channel.API)
    assert [r.id for r in world.service.list_requests()] == [first.id, second.id]


# ---------------------------------------- Events (the dashboard's feed)


def test_every_change_is_an_event_including_requests_it_made_stale(world: World) -> None:
    a = world.raise_ganger()
    b = world.raise_ganger(by=world.sam, new="23.00")
    world.service.approve(a.id, by=world.priya, via=Channel.API)
    world.service.decline(b.id, by=world.tom, reason="Overtaken", via=Channel.API)

    events = world.service.events_after(0)
    assert [(e.type, e.request_id) for e in events] == [
        (EventType.RAISED, a.id),
        (EventType.RAISED, b.id),
        (EventType.APPROVED, a.id),
        (EventType.UPDATED, b.id),  # approving a made b stale; the wall must show it
        (EventType.DECLINED, b.id),
    ]
    assert events[2].payload["outcome"]["new_rate"] == "24.00"  # type: ignore[index]
    assert events[3].payload["stale"] is True
    assert events[3].payload["rate_in_force"] == "24.00"


def test_a_refused_change_writes_no_event(world: World) -> None:
    own = world.raise_ganger(by=world.priya)
    before = world.service.latest_event_id()
    with pytest.raises(FourEyesViolation):
        world.service.approve(own.id, by=world.priya, via=Channel.API)
    with pytest.raises(RateMismatch):
        world.raise_ganger(now="99.00")
    assert world.service.latest_event_id() == before


def test_an_approval_and_its_event_commit_together_or_not_at_all(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = world.raise_ganger()

    def broken(*args: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr("rate_desk.service._append_event", broken)
    with pytest.raises(RuntimeError):
        world.service.approve(request.id, by=world.priya, via=Channel.API)

    # The rate change was rolled back along with the missing event.
    assert world.service.get_request(request.id).status is Status.PENDING
    assert in_force(world) == {"ganger": Money(2250)}
    assert len(world.service.rate_history("viaduct", "ganger")) == 1
