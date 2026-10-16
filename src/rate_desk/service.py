"""The rules of Rate Desk, in one place.

The REST API, the MCP server and the command line are all thin adapters
over :class:`RateDesk`. None of them touch the database or decide anything
themselves, so a rule cannot be enforced in one door and forgotten in another.

Every change runs in one SQLite write transaction that both makes the change
and appends the event describing it. Either both happen or neither does.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from rate_desk.db import Database, from_db_time, to_db_time
from rate_desk.domain import (
    Actor,
    Channel,
    Event,
    EventType,
    Outcome,
    Project,
    Rate,
    RateChangeRequest,
    Status,
    Trade,
    User,
)
from rate_desk.errors import (
    AlreadyDecided,
    FourEyesViolation,
    InvalidRequest,
    NotFound,
    NotPermitted,
    RateMismatch,
    StaleRequest,
    Unauthenticated,
)
from rate_desk.money import Money
from rate_desk.schemas import RequestOut

MAX_TEXT = 2000


def utc_now() -> datetime:
    return datetime.now(UTC)


def hash_token(token: str) -> str:
    # Tokens are 256 random bits, so a fast hash is enough: there is nothing
    # to brute-force. We store the hash so a copy of the database is not a
    # copy of everyone's credentials.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What the wall dashboard needs on connect, all read from one consistent snapshot:
    every pending request, and the most recent decisions (up to 1,000)."""

    cursor: int  # id of the newest event already reflected below
    pending: list[RateChangeRequest]
    recently_decided: list[RateChangeRequest]
    decided_total: int  # all decided requests, including any beyond the ones listed


class RateDesk:
    def __init__(
        self,
        db: Database,
        *,
        clock: Callable[[], datetime] = utc_now,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        # Called after every committed change, so live streams can wake up at once.
        self.on_change = on_change

    # ------------------------------------------------------------------ identity

    def authenticate(self, token: str) -> User:
        with self._db.read() as conn:
            row = conn.execute(
                f"{_USER_COLUMNS} WHERE token_sha256 = ?",
                (hash_token(token),),
            ).fetchone()
        if row is None:
            raise Unauthenticated("unknown token")
        return _user(row)

    def has_users(self) -> bool:
        with self._db.read() as conn:
            return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def get_user(self, user_id: int) -> User:
        with self._db.read() as conn:
            row = conn.execute(f"{_USER_COLUMNS} WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise Unauthenticated("that user no longer exists")
        return _user(row)

    def add_user(
        self,
        name: str,
        *,
        can_raise: bool,
        can_approve: bool,
        can_act_as_others: bool = False,
        token: str | None = None,
    ) -> tuple[User, str]:
        """Create a user and return their token. The token is not stored, only its hash."""
        token = token or "rd_" + secrets.token_urlsafe(32)
        with self._db.write() as conn:
            cursor = conn.execute(
                "INSERT INTO users (name, token_sha256, can_raise, can_approve, can_act_as_others)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, hash_token(token), int(can_raise), int(can_approve), int(can_act_as_others)),
            )
        assert cursor.lastrowid is not None
        user = User(cursor.lastrowid, name, can_raise, can_approve, can_act_as_others)
        return user, token

    def has_tester(self) -> bool:
        with self._db.read() as conn:
            row = conn.execute("SELECT 1 FROM users WHERE can_act_as_others = 1 LIMIT 1")
            return row.fetchone() is not None

    def people(self, *, caller: User) -> list[User]:
        """Everyone the demo tester can act as."""
        _require_tester(caller)
        with self._db.read() as conn:
            rows = conn.execute(f"{_USER_COLUMNS} WHERE can_act_as_others = 0 ORDER BY id")
            return [_user(row) for row in rows.fetchall()]

    def act_as(self, caller: User, user_id: int) -> Actor:
        """The demo tester stepping into someone else's shoes, with only that person's rights."""
        _require_tester(caller)
        with self._db.read() as conn:
            row = conn.execute(f"{_USER_COLUMNS} WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise NotFound(f"There is no person #{user_id} to act as")
        target = _user(row)
        if target.can_act_as_others:
            raise InvalidRequest("a tester can only act as the ordinary demo people")
        return Actor(target, proxy=caller)

    # ------------------------------------------------------------ reference data

    def add_project(self, code: str, name: str) -> Project:
        with self._db.write() as conn:
            conn.execute("INSERT INTO projects (code, name) VALUES (?, ?)", (code, name))
        return Project(code, name)

    def add_trade(self, code: str, name: str) -> Trade:
        with self._db.write() as conn:
            conn.execute("INSERT INTO trades (code, name) VALUES (?, ?)", (code, name))
        return Trade(code, name)

    def list_projects(self) -> list[Project]:
        with self._db.read() as conn:
            rows = conn.execute("SELECT code, name FROM projects ORDER BY name").fetchall()
        return [Project(r["code"], r["name"]) for r in rows]

    def list_trades(self) -> list[Trade]:
        with self._db.read() as conn:
            rows = conn.execute("SELECT code, name FROM trades ORDER BY name").fetchall()
        return [Trade(r["code"], r["name"]) for r in rows]

    def load_opening_rate(self, project: str, trade: str, amount: Money) -> None:
        """Set up the first rate for a project and trade, before Rate Desk takes over.

        This is the one write to ``rates`` that does not come from an approval,
        so it only works where that project and trade have no rate history at
        all. It can start a rate. It can never change one.
        """
        with self._db.write() as conn:
            _require_project(conn, project)
            _require_trade(conn, trade)
            existing = conn.execute(
                "SELECT 1 FROM rates WHERE project_code = ? AND trade_code = ?", (project, trade)
            ).fetchone()
            if existing is not None:
                raise InvalidRequest(
                    f"{trade} on {project} already has a rate; it can only change by approval"
                )
            conn.execute(
                "INSERT INTO rates (project_code, trade_code, amount_pence, effective_from)"
                " VALUES (?, ?, ?, ?)",
                (project, trade, amount.pence, to_db_time(self._now())),
            )

    # ------------------------------------------------------------------ requests

    def raise_request(
        self,
        *,
        by: User,
        project: str,
        trade: str,
        current: Money | None,
        requested: Money,
        reason: str,
        proxy: User | None = None,
    ) -> RateChangeRequest:
        """Record a request. Rule 1: this writes no rate. Nothing changes until approval."""
        _check_proxy(by, proxy)
        if not by.can_raise:
            raise NotPermitted(f"{by.name} is not allowed to raise rate changes")
        reason = _required_text(reason, "a reason")
        if requested.pence <= 0:
            raise InvalidRequest("the rate asked for must be more than £0.00")
        if current is not None and current == requested:
            raise InvalidRequest(f"the rate asked for is the same as the rate now ({current})")

        with self._db.write() as conn:
            now = to_db_time(self._now())
            project_name = _require_project(conn, project)
            trade_name = _require_trade(conn, trade)
            in_force = _amount_in_force(conn, project, trade)
            if in_force != current:
                raise RateMismatch(
                    f"The rate in force for {trade_name} on {project_name} is "
                    f"{_describe(in_force)}, not {_describe(current)}. "
                    "Raise the request against the rate actually in force."
                )
            cursor = conn.execute(
                "INSERT INTO rate_change_requests"
                " (project_code, trade_code, current_pence, requested_pence, reason,"
                "  requested_by, requested_at, raised_by_proxy)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project,
                    trade,
                    None if current is None else current.pence,
                    requested.pence,
                    reason,
                    by.id,
                    now,
                    None if proxy is None else proxy.id,
                ),
            )
            assert cursor.lastrowid is not None
            raised = _load_request(conn, cursor.lastrowid)
            _append_event(conn, EventType.RAISED, raised, now)
        self._changed()
        return raised

    def get_request(self, request_id: int) -> RateChangeRequest:
        with self._db.read() as conn:
            return _load_request(conn, request_id)

    def list_requests(
        self,
        *,
        status: Status | None = Status.PENDING,
        project: str | None = None,
        requested_by: int | None = None,
        limit: int = 200,
    ) -> list[RateChangeRequest]:
        """Pending requests come oldest first (a queue); anything else newest first (a log)."""
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("r.status = ?")
            params.append(status.value)
        if project is not None:
            clauses.append("r.project_code = ?")
            params.append(project)
        if requested_by is not None:
            clauses.append("r.requested_by = ?")
            params.append(requested_by)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "ASC" if status is Status.PENDING else "DESC"
        with self._db.read() as conn:
            rows = conn.execute(
                f"{_REQUEST_SELECT} {where} ORDER BY r.id {order} LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [_request(row) for row in rows]

    def check_approvable(self, request_id: int, *, by: User) -> RateChangeRequest:
        """Raise exactly what :meth:`approve` would raise right now, without approving.

        Used to avoid asking a human to confirm something that would fail.
        ``approve`` repeats every check inside its own transaction regardless.
        """
        with self._db.read() as conn:
            request = _load_request(conn, request_id)
        _check_approvable(request, by)
        return request

    def check_declinable(self, request_id: int, *, by: User, reason: str) -> RateChangeRequest:
        """Raise exactly what :meth:`decline` would raise right now, without declining."""
        _required_text(reason, "a reason for declining")
        with self._db.read() as conn:
            request = _load_request(conn, request_id)
        _check_decidable(request, by, "decline")
        return request

    def approve(
        self, request_id: int, *, by: User, via: Channel, proxy: User | None = None
    ) -> RateChangeRequest:
        """Approve a request. The new rate takes effect now, in this transaction, and not before.

        Rule 3 is enforced four times over: the status check below runs while
        holding SQLite's write lock; the UPDATE only matches a pending row; the
        rates table allows one rate per source request; and a trigger refuses
        any change to a decided request.
        """
        _check_proxy(by, proxy)
        with self._db.write() as conn:
            request = _load_request(conn, request_id)
            _check_approvable(request, by)
            now = to_db_time(self._now())
            in_force = conn.execute(
                "SELECT id FROM rates"
                " WHERE project_code = ? AND trade_code = ? AND superseded_at IS NULL",
                (request.project.code, request.trade.code),
            ).fetchone()
            previous_rate_id: int | None = None if in_force is None else in_force["id"]
            if previous_rate_id is not None:
                conn.execute(
                    "UPDATE rates SET superseded_at = ? WHERE id = ?", (now, previous_rate_id)
                )
            inserted = conn.execute(
                "INSERT INTO rates"
                " (project_code, trade_code, amount_pence, effective_from, source_request_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    request.project.code,
                    request.trade.code,
                    request.requested.pence,
                    now,
                    request.id,
                ),
            )
            updated = conn.execute(
                "UPDATE rate_change_requests"
                " SET status = 'approved', decided_by = ?, decided_at = ?, decided_via = ?,"
                "     previous_rate_id = ?, resulting_rate_id = ?, decided_by_proxy = ?"
                " WHERE id = ? AND status = 'pending'",
                (
                    by.id,
                    now,
                    via.value,
                    previous_rate_id,
                    inserted.lastrowid,
                    None if proxy is None else proxy.id,
                    request.id,
                ),
            )
            if updated.rowcount != 1:  # unreachable while we hold the write lock
                raise AlreadyDecided(f"Request #{request.id} has already been decided")
            approved = _load_request(conn, request.id)
            _append_event(conn, EventType.APPROVED, approved, now)
            # Any other request waiting on this project and trade was raised against
            # the old rate and is now stale. Tell the dashboard, in the same transaction.
            overtaken = conn.execute(
                "SELECT id FROM rate_change_requests"
                " WHERE project_code = ? AND trade_code = ? AND status = 'pending'",
                (request.project.code, request.trade.code),
            ).fetchall()
            for row in overtaken:
                _append_event(conn, EventType.UPDATED, _load_request(conn, row["id"]), now)
        self._changed()
        return approved

    def decline(
        self,
        request_id: int,
        *,
        by: User,
        reason: str,
        via: Channel,
        proxy: User | None = None,
    ) -> RateChangeRequest:
        """Decline a request, with a reason the person who raised it will see. No rate changes."""
        _check_proxy(by, proxy)
        reason = _required_text(reason, "a reason for declining")
        with self._db.write() as conn:
            request = _load_request(conn, request_id)
            _check_decidable(request, by, "decline")
            now = to_db_time(self._now())
            updated = conn.execute(
                "UPDATE rate_change_requests"
                " SET status = 'declined', decided_by = ?, decided_at = ?, decided_via = ?,"
                "     decline_reason = ?, decided_by_proxy = ?"
                " WHERE id = ? AND status = 'pending'",
                (by.id, now, via.value, reason, None if proxy is None else proxy.id, request.id),
            )
            if updated.rowcount != 1:  # unreachable while we hold the write lock
                raise AlreadyDecided(f"Request #{request.id} has already been decided")
            declined = _load_request(conn, request.id)
            _append_event(conn, EventType.DECLINED, declined, now)
        self._changed()
        return declined

    # --------------------------------------------------------------------- rates

    def rates_in_force(
        self, project: str, *, as_of: datetime | None = None
    ) -> tuple[Project, datetime, list[Rate]]:
        """The rates in force for a project now, or at any moment in the past."""
        moment = self._now() if as_of is None else as_of
        at = to_db_time(moment)
        with self._db.read() as conn:
            project_name = _require_project(conn, project)
            rows = conn.execute(
                f"{_RATE_SELECT}"
                " WHERE x.project_code = ? AND x.effective_from <= ?"
                "   AND (x.superseded_at IS NULL OR x.superseded_at > ?)"
                " ORDER BY t.name",
                (project, at, at),
            ).fetchall()
        return Project(project, project_name), moment, [_rate(row) for row in rows]

    def rate_history(self, project: str, trade: str) -> list[Rate]:
        with self._db.read() as conn:
            _require_project(conn, project)
            _require_trade(conn, trade)
            rows = conn.execute(
                f"{_RATE_SELECT} WHERE x.project_code = ? AND x.trade_code = ? ORDER BY x.id",
                (project, trade),
            ).fetchall()
        return [_rate(row) for row in rows]

    # -------------------------------------------------------------------- events

    def latest_event_id(self) -> int:
        with self._db.read() as conn:
            return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])

    def events_after(self, event_id: int, *, limit: int = 500) -> list[Event]:
        """Events newer than ``event_id``, oldest first.

        Safe to page through by id only because SQLite has a single writer:
        ids are handed out in commit order, so a reader can never see event
        N+1 before event N has committed. (On Postgres that is not true; see
        DECISIONS.md.)
        """
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT id, type, request_id, occurred_at, payload FROM events"
                " WHERE id > ? ORDER BY id LIMIT ?",
                (event_id, limit),
            ).fetchall()
        return [
            Event(
                id=row["id"],
                type=EventType(row["type"]),
                request_id=row["request_id"],
                occurred_at=from_db_time(row["occurred_at"]),
                payload=json.loads(row["payload"]),
            )
            for row in rows
        ]

    def snapshot(self, *, recent: int = 1000) -> Snapshot:
        with self._db.read() as conn:
            cursor = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
            pending = conn.execute(
                f"{_REQUEST_SELECT} WHERE r.status = 'pending' ORDER BY r.id ASC"
            ).fetchall()
            decided = conn.execute(
                f"{_REQUEST_SELECT} WHERE r.status <> 'pending' ORDER BY r.decided_at DESC LIMIT ?",
                (recent,),
            ).fetchall()
            decided_total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM rate_change_requests WHERE status <> 'pending'"
                ).fetchone()[0]
            )
        return Snapshot(
            cursor, [_request(r) for r in pending], [_request(r) for r in decided], decided_total
        )

    # ------------------------------------------------------------------ internal

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("the clock must return timezone-aware times")
        return now

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()


# ---------------------------------------------------------------------- helpers


def _require_tester(caller: User) -> None:
    if not caller.can_act_as_others:
        raise NotPermitted(f"{caller.name} cannot act as anyone else")


def _check_proxy(by: User, proxy: User | None) -> None:
    """Acting as someone else needs the tester's capability, and only reaches ordinary people.

    The API checks this when it builds the Actor; checking again here keeps the
    rule true for any other caller of the service.
    """
    if proxy is None:
        return
    _require_tester(proxy)
    if by.can_act_as_others:
        raise InvalidRequest("a tester can only act as the ordinary demo people")


def _check_approvable(request: RateChangeRequest, by: User) -> None:
    _check_decidable(request, by, "approve")
    if request.rate_in_force != request.current:
        raise StaleRequest(
            f"Request #{request.id} was raised against {_describe(request.current)}, but "
            f"the rate in force for {request.trade.name} on {request.project.name} is now "
            f"{_describe(request.rate_in_force)}. Approving it would not do what it says. "
            "Decline it and ask for a new request against the current rate."
        )


def _check_decidable(request: RateChangeRequest, by: User, verb: str) -> None:
    """Who may decide, and whether there is anything left to decide."""
    if not by.can_approve:
        raise NotPermitted(f"{by.name} is not allowed to {verb} rate changes")
    if request.status is not Status.PENDING:
        assert request.decided_by is not None
        raise AlreadyDecided(
            f"Request #{request.id} was already {request.status.value} by "
            f"{request.decided_by.name}; it cannot be {verb}d again"
        )
    if request.requested_by.id == by.id:
        raise FourEyesViolation(
            f"Request #{request.id} was raised by you. Somebody else has to {verb} it."
        )


def _required_text(text: str, what: str) -> str:
    text = text.strip()
    # Control characters (NUL above all: SQLite stops counting at it) are refused,
    # apart from line breaks and tabs, which a written reason can contain.
    if any(unicodedata.category(c) == "Cc" and c not in "\n\r\t" for c in text):
        raise InvalidRequest(f"{what} contains control characters")
    # Invisible text, such as only zero-width spaces, counts as empty.
    if not any(not c.isspace() and unicodedata.category(c) != "Cf" for c in text):
        raise InvalidRequest(f"{what} is required")
    if len(text) > MAX_TEXT:
        raise InvalidRequest(f"{what} must be at most {MAX_TEXT} characters")
    return text


def _describe(amount: Money | None) -> str:
    return "no rate yet" if amount is None else amount.display()


def _require_project(conn: sqlite3.Connection, code: str) -> str:
    row = conn.execute("SELECT name FROM projects WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise NotFound(f"There is no project {code!r}")
    return str(row["name"])


def _require_trade(conn: sqlite3.Connection, code: str) -> str:
    row = conn.execute("SELECT name FROM trades WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise NotFound(f"There is no trade {code!r}")
    return str(row["name"])


def _amount_in_force(conn: sqlite3.Connection, project: str, trade: str) -> Money | None:
    row = conn.execute(
        "SELECT amount_pence FROM rates"
        " WHERE project_code = ? AND trade_code = ? AND superseded_at IS NULL",
        (project, trade),
    ).fetchone()
    return None if row is None else Money(row["amount_pence"])


def _append_event(
    conn: sqlite3.Connection, kind: EventType, request: RateChangeRequest, at: str
) -> None:
    # The payload is the same JSON the REST API returns for the request, at
    # the moment of the event, so the dashboard has one shape to render.
    payload = RequestOut.from_domain(request).model_dump_json()
    conn.execute(
        "INSERT INTO events (type, request_id, occurred_at, payload) VALUES (?, ?, ?, ?)",
        (kind.value, request.id, at, payload),
    )


_REQUEST_SELECT = """
SELECT r.*,
       p.name AS project_name,
       t.name AS trade_name,
       ru.name AS requester_name, ru.can_raise AS requester_can_raise,
       ru.can_approve AS requester_can_approve,
       du.name AS decider_name, du.can_raise AS decider_can_raise,
       du.can_approve AS decider_can_approve,
       cur.amount_pence AS in_force_pence,
       prev.amount_pence AS previous_pence,
       res.amount_pence AS resulting_pence,
       res.effective_from AS resulting_effective_from,
       res.superseded_at AS resulting_superseded_at,
       nxt.id AS superseded_by_request_id,
       rp.name AS raised_by_proxy_name,
       dp.name AS decided_by_proxy_name
FROM rate_change_requests r
JOIN projects p ON p.code = r.project_code
JOIN trades t ON t.code = r.trade_code
JOIN users ru ON ru.id = r.requested_by
LEFT JOIN users du ON du.id = r.decided_by
LEFT JOIN rates cur
       ON cur.project_code = r.project_code AND cur.trade_code = r.trade_code
      AND cur.superseded_at IS NULL
LEFT JOIN rates prev ON prev.id = r.previous_rate_id
LEFT JOIN rates res ON res.id = r.resulting_rate_id
LEFT JOIN rate_change_requests nxt ON nxt.previous_rate_id = r.resulting_rate_id
LEFT JOIN users rp ON rp.id = r.raised_by_proxy
LEFT JOIN users dp ON dp.id = r.decided_by_proxy
"""

_RATE_SELECT = """
SELECT x.*, p.name AS project_name, t.name AS trade_name
FROM rates x
JOIN projects p ON p.code = x.project_code
JOIN trades t ON t.code = x.trade_code
"""


def _load_request(conn: sqlite3.Connection, request_id: int) -> RateChangeRequest:
    row = conn.execute(f"{_REQUEST_SELECT} WHERE r.id = ?", (request_id,)).fetchone()
    if row is None:
        raise NotFound(f"There is no request #{request_id}")
    return _request(row)


_USER_COLUMNS = "SELECT id, name, can_raise, can_approve, can_act_as_others FROM users"


def _user(row: sqlite3.Row) -> User:
    return User(
        row["id"],
        row["name"],
        bool(row["can_raise"]),
        bool(row["can_approve"]),
        bool(row["can_act_as_others"]),
    )


def _proxy(row: sqlite3.Row, column: str) -> User | None:
    """The tester behind an action, from the joined columns; None for a normal action."""
    if row[column] is None:
        return None
    return User(row[column], row[f"{column}_name"], False, False, can_act_as_others=True)


def _optional_time(text: str | None) -> datetime | None:
    return None if text is None else from_db_time(text)


def _request(row: sqlite3.Row) -> RateChangeRequest:
    status = Status(row["status"])
    outcome = None
    if status is Status.APPROVED:
        outcome = Outcome(
            previous_rate_id=row["previous_rate_id"],
            previous_amount=None if row["previous_pence"] is None else Money(row["previous_pence"]),
            resulting_rate_id=row["resulting_rate_id"],
            resulting_amount=Money(row["resulting_pence"]),
            effective_from=from_db_time(row["resulting_effective_from"]),
            superseded_at=_optional_time(row["resulting_superseded_at"]),
            superseded_by_request_id=row["superseded_by_request_id"],
        )
    decided_by = None
    if row["decided_by"] is not None:
        decided_by = User(
            row["decided_by"],
            row["decider_name"],
            bool(row["decider_can_raise"]),
            bool(row["decider_can_approve"]),
        )
    return RateChangeRequest(
        id=row["id"],
        project=Project(row["project_code"], row["project_name"]),
        trade=Trade(row["trade_code"], row["trade_name"]),
        current=None if row["current_pence"] is None else Money(row["current_pence"]),
        requested=Money(row["requested_pence"]),
        reason=row["reason"],
        requested_by=User(
            row["requested_by"],
            row["requester_name"],
            bool(row["requester_can_raise"]),
            bool(row["requester_can_approve"]),
        ),
        requested_at=from_db_time(row["requested_at"]),
        status=status,
        decided_by=decided_by,
        decided_at=_optional_time(row["decided_at"]),
        decided_via=None if row["decided_via"] is None else Channel(row["decided_via"]),
        decline_reason=row["decline_reason"],
        outcome=outcome,
        raised_by_proxy=_proxy(row, "raised_by_proxy"),
        decided_by_proxy=_proxy(row, "decided_by_proxy"),
        rate_in_force=None if row["in_force_pence"] is None else Money(row["in_force_pence"]),
    )


def _rate(row: sqlite3.Row) -> Rate:
    return Rate(
        id=row["id"],
        project=Project(row["project_code"], row["project_name"]),
        trade=Trade(row["trade_code"], row["trade_name"]),
        amount=Money(row["amount_pence"]),
        effective_from=from_db_time(row["effective_from"]),
        superseded_at=_optional_time(row["superseded_at"]),
        source_request_id=row["source_request_id"],
    )
