"""The things Rate Desk talks about. Plain immutable records; no behaviour that touches storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from rate_desk.money import Money, percent_change


class Status(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"


class Channel(StrEnum):
    """How a decision arrived, so the audit trail shows when an assistant was involved."""

    API = "api"
    MCP = "mcp"


class EventType(StrEnum):
    RAISED = "request.raised"
    APPROVED = "request.approved"
    DECLINED = "request.declined"
    # A pending request changed without being decided: it went stale.
    UPDATED = "request.updated"


@dataclass(frozen=True, slots=True)
class User:
    id: int
    name: str
    can_raise: bool
    can_approve: bool


@dataclass(frozen=True, slots=True)
class Project:
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class Trade:
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class Rate:
    """One row of rate history. Immutable once written, apart from being superseded once."""

    id: int
    project: Project
    trade: Trade
    amount: Money
    effective_from: datetime
    superseded_at: datetime | None
    source_request_id: int | None  # None for the opening rates loaded at setup


@dataclass(frozen=True, slots=True)
class Outcome:
    """Rule 2: what an approval actually did, readable for ever after."""

    previous_rate_id: int | None
    previous_amount: Money | None
    resulting_rate_id: int
    resulting_amount: Money
    effective_from: datetime
    superseded_at: datetime | None
    superseded_by_request_id: int | None

    @property
    def still_in_force(self) -> bool:
        return self.superseded_at is None


@dataclass(frozen=True, slots=True)
class RateChangeRequest:
    id: int
    project: Project
    trade: Trade
    current: Money | None  # "the rate now" as the requester stated it; None = no rate yet
    requested: Money
    reason: str
    requested_by: User
    requested_at: datetime
    status: Status
    decided_by: User | None
    decided_at: datetime | None
    decided_via: Channel | None
    decline_reason: str | None
    outcome: Outcome | None
    # The rate in force for this project and trade right now, whatever the request says.
    rate_in_force: Money | None

    @property
    def change(self) -> Money | None:
        """By how much the rate would move, or did move. None for a first-ever rate."""
        return None if self.current is None else self.requested - self.current

    @property
    def percent(self) -> Decimal | None:
        return None if self.current is None else percent_change(self.current, self.requested)

    @property
    def is_stale(self) -> bool:
        """A pending request whose "rate now" is no longer the rate in force cannot be approved."""
        return self.status is Status.PENDING and self.rate_in_force != self.current


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    type: EventType
    request_id: int
    occurred_at: datetime
    payload: dict[str, object]
