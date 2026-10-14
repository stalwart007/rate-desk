"""The shapes Rate Desk shows to the outside world.

One representation of a request is used everywhere: REST responses, the
events streamed to the wall dashboard, and the structured output of MCP
tools. Money crosses this boundary as decimal strings ("18.50"), never as
JSON numbers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rate_desk.domain import Channel, Outcome, Rate, RateChangeRequest, Status, User
from rate_desk.money import AMOUNT_PATTERN, Money


def _money(value: Money | None) -> str | None:
    return None if value is None else value.to_decimal_string()


def _person(user: User | None) -> PersonOut | None:
    return None if user is None else PersonOut.from_domain(user)


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class PersonOut(_Out):
    id: int
    name: str

    @classmethod
    def from_domain(cls, user: User) -> PersonOut:
        return cls(id=user.id, name=user.name)


class MeOut(PersonOut):
    can_raise: bool
    can_approve: bool
    can_act_as_others: bool = Field(
        description="Demo tester only: may act as the other demo people (X-Act-As header)."
    )

    @classmethod
    def from_domain(cls, user: User) -> MeOut:
        return cls(
            id=user.id,
            name=user.name,
            can_raise=user.can_raise,
            can_approve=user.can_approve,
            can_act_as_others=user.can_act_as_others,
        )


class OutcomeOut(_Out):
    """What an approval did (Rule 2)."""

    previous_rate: str | None = Field(
        description="The rate it replaced; null if it set a first rate."
    )
    new_rate: str
    previous_rate_id: int | None
    resulting_rate_id: int
    effective_from: datetime
    still_in_force: bool
    superseded_at: datetime | None
    superseded_by_request_id: int | None

    @classmethod
    def from_domain(cls, outcome: Outcome) -> OutcomeOut:
        return cls(
            previous_rate=_money(outcome.previous_amount),
            new_rate=outcome.resulting_amount.to_decimal_string(),
            previous_rate_id=outcome.previous_rate_id,
            resulting_rate_id=outcome.resulting_rate_id,
            effective_from=outcome.effective_from,
            still_in_force=outcome.still_in_force,
            superseded_at=outcome.superseded_at,
            superseded_by_request_id=outcome.superseded_by_request_id,
        )


class RequestOut(_Out):
    id: int
    status: Status
    project: str
    project_name: str
    trade: str
    trade_name: str
    currency: Literal["GBP"] = "GBP"
    unit: Literal["hour"] = "hour"
    current_rate: str | None = Field(description="The rate now, as stated by the requester.")
    requested_rate: str
    change: str | None = Field(description="requested_rate minus current_rate.")
    change_percent: str | None = Field(description="Rounded half-up to 1 dp. Display only.")
    reason: str
    requested_by: PersonOut
    requested_at: datetime
    decided_by: PersonOut | None
    decided_at: datetime | None
    decided_via: Channel | None
    decline_reason: str | None
    outcome: OutcomeOut | None
    rate_in_force: str | None = Field(
        description="The rate in force right now for this project and trade."
    )
    stale: bool = Field(
        description="Pending, but the rate in force is no longer the rate it was raised against."
    )
    raised_by_proxy: PersonOut | None = Field(
        description="The demo tester, if they raised this acting as requested_by. Usually null."
    )
    decided_by_proxy: PersonOut | None = Field(
        description="The demo tester, if they decided this acting as decided_by. Usually null."
    )

    @classmethod
    def from_domain(cls, r: RateChangeRequest) -> RequestOut:
        return cls(
            id=r.id,
            status=r.status,
            project=r.project.code,
            project_name=r.project.name,
            trade=r.trade.code,
            trade_name=r.trade.name,
            current_rate=_money(r.current),
            requested_rate=r.requested.to_decimal_string(),
            change=_money(r.change),
            change_percent=None if r.percent is None else str(r.percent),
            reason=r.reason,
            requested_by=PersonOut.from_domain(r.requested_by),
            requested_at=r.requested_at,
            decided_by=None if r.decided_by is None else PersonOut.from_domain(r.decided_by),
            decided_at=r.decided_at,
            decided_via=r.decided_via,
            decline_reason=r.decline_reason,
            outcome=None if r.outcome is None else OutcomeOut.from_domain(r.outcome),
            rate_in_force=_money(r.rate_in_force),
            stale=r.is_stale,
            raised_by_proxy=_person(r.raised_by_proxy),
            decided_by_proxy=_person(r.decided_by_proxy),
        )


class RateOut(_Out):
    id: int
    project: str
    trade: str
    trade_name: str
    currency: Literal["GBP"] = "GBP"
    unit: Literal["hour"] = "hour"
    rate: str
    effective_from: datetime
    superseded_at: datetime | None
    set_by_request_id: int | None = Field(description="null for an opening rate loaded at setup.")

    @classmethod
    def from_domain(cls, rate: Rate) -> RateOut:
        return cls(
            id=rate.id,
            project=rate.project.code,
            trade=rate.trade.code,
            trade_name=rate.trade.name,
            rate=rate.amount.to_decimal_string(),
            effective_from=rate.effective_from,
            superseded_at=rate.superseded_at,
            set_by_request_id=rate.source_request_id,
        )


class ProjectRatesOut(_Out):
    project: str
    project_name: str
    as_of: datetime
    rates: list[RateOut]


class NamedOut(_Out):
    code: str
    name: str


class RaiseRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project: str = Field(min_length=1, max_length=64, examples=["riverside-viaduct"])
    trade: str = Field(min_length=1, max_length=64, examples=["ganger"])
    # strict: a JSON number is refused rather than quietly turned into text.
    current_rate: str | None = Field(
        default=None,
        strict=True,
        pattern=AMOUNT_PATTERN,
        examples=["22.50"],
        description="The rate in force now, as you understand it. Omit only when the trade "
        "has no rate on this project yet.",
    )
    requested_rate: str = Field(
        strict=True,
        pattern=AMOUNT_PATTERN,
        examples=["24.00"],
        description="The rate you are asking for, in pounds per hour.",
    )
    reason: str = Field(min_length=1, max_length=2000)


class DeclineIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(
        min_length=1, max_length=2000, description="Shown to the person who raised the request."
    )


class ErrorOut(_Out):
    code: str
    message: str
