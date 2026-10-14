"""HTTP: the REST API, the live stream for the wall, the dashboard page, and the MCP endpoint.

Everything is served by one process, so there is one thing to deploy, one
port, and one place the database is opened. Handlers are thin: parse,
authenticate, call the service, shape the answer.
"""

# No `from __future__ import annotations`: FastAPI reads handler annotations at
# startup, and `CurrentUser` / `CurrentActor` are defined inside create_app, out
# of reach of string annotations.

import asyncio
import contextlib
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from importlib.resources import files
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.sse import EventSourceResponse, ServerSentEvent
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rate_desk import explain
from rate_desk.config import Settings
from rate_desk.db import Database
from rate_desk.domain import Actor, Channel, Event, Status, User
from rate_desk.errors import InvalidRequest, NotPermitted, RateDeskError, Unauthenticated
from rate_desk.mcp_server import build_mcp_server
from rate_desk.money import Money
from rate_desk.schemas import (
    DeclineIn,
    ErrorOut,
    MeOut,
    NamedOut,
    ProjectRatesOut,
    RaiseRequestIn,
    RateOut,
    RequestOut,
)
from rate_desk.service import RateDesk, Snapshot, utc_now
from rate_desk.stream import Broadcaster

log = logging.getLogger("rate_desk.http")

ERROR_STATUS = {
    "unauthenticated": 401,
    "not_permitted": 403,
    "four_eyes": 403,
    "not_found": 404,
    "already_decided": 409,
    "rate_mismatch": 409,
    "stale": 409,
    "invalid": 422,
}

# Ask browsers to retry quickly after a dropped connection; a wall display
# should be back within seconds of the network or the server returning.
RETRY_MS = 2000
# A named event the page can see (keep-alive comments never reach JavaScript),
# so a screen whose connection died silently can notice and reconnect.
HEARTBEAT_SECONDS = 15.0

# SQLite integers are 64-bit; a bigger id is not "not found", it is nonsense.
RequestId = Annotated[int, Path(ge=1, le=2**63 - 1)]

ERRORS: dict[int | str, dict[str, object]] = {
    status: {"model": ErrorOut} for status in (401, 403, 404, 409)
}


class SnapshotOut(BaseModel):
    cursor: int
    pending: list[RequestOut]
    recently_decided: list[RequestOut]

    @classmethod
    def from_domain(cls, snap: Snapshot) -> "SnapshotOut":
        return cls(
            cursor=snap.cursor,
            pending=[RequestOut.from_domain(r) for r in snap.pending],
            recently_decided=[RequestOut.from_domain(r) for r in snap.recently_decided],
        )


class EventOut(BaseModel):
    event_id: int
    type: str
    occurred_at: datetime
    request: dict[str, object]  # a RequestOut, as it stood at the moment of the event


def _sse(event: Event) -> ServerSentEvent:
    return ServerSentEvent(
        event=event.type.value,
        id=str(event.id),
        data=EventOut(
            event_id=event.id,
            type=event.type.value,
            occurred_at=event.occurred_at,
            request=event.payload,
        ),
    )


def create_app(
    settings: Settings,
    *,
    clock: Callable[[], datetime] = utc_now,
    poll_interval: float = 1.0,
) -> FastAPI:
    db = Database(settings.db_path)
    db.migrate()
    service = RateDesk(db, clock=clock)
    if settings.demo:
        from rate_desk.seed import seed_demo

        seed_demo(service, only_if_empty=True)

    broadcaster = Broadcaster(service, poll_interval=poll_interval)
    service.on_change = broadcaster.nudge

    mcp = build_mcp_server(service, public_url=settings.public_url)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            allowed_hosts=settings.allowed_hosts, allowed_origins=settings.allowed_origins
        ),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            # Before accepting connections, so no screen can subscribe ahead of the tailer.
            await broadcaster.start()
            tailer = asyncio.create_task(broadcaster.run(), name="event-tailer")
            try:
                yield
            finally:
                tailer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tailer

    app = FastAPI(
        title="Rate Desk",
        version="1.0.0",
        summary="Pay-rate changes: raised by one person, approved by another, applied only then.",
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.broadcaster = broadcaster

    @app.exception_handler(RequestValidationError)
    async def invalid_input(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'][1:]) or 'body'}: {error['msg']}"
            for error in exc.errors()
        )
        return JSONResponse(ErrorOut(code="invalid", message=problems).model_dump(), 422)

    @app.exception_handler(sqlite3.IntegrityError)
    async def refused_by_database(_: Request, exc: sqlite3.IntegrityError) -> JSONResponse:
        # The service should have refused first. If it did not, the database's
        # own constraints did, and nothing was written.
        log.error("database constraint refused a change: %s", exc)
        message = f"the database refused this change ({exc}); nothing was written"
        return JSONResponse(ErrorOut(code="conflict", message=message).model_dump(), 409)

    @app.exception_handler(RateDeskError)
    async def rate_desk_error(_: Request, exc: RateDeskError) -> JSONResponse:
        status = ERROR_STATUS.get(exc.code, 400)
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        return JSONResponse(
            ErrorOut(code=exc.code, message=exc.message).model_dump(), status, headers=headers
        )

    # --------------------------------------------------------------- identity

    bearer = HTTPBearer(auto_error=False, description="Your Rate Desk token.")

    def current_user(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> User:
        if credentials is None:
            raise Unauthenticated("send your token as 'Authorization: Bearer <token>'")
        return service.authenticate(credentials.credentials)

    def stream_user(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        access_token: Annotated[
            str | None,
            Query(description="For browsers: EventSource cannot send headers."),
        ] = None,
    ) -> User:
        token = credentials.credentials if credentials else access_token
        if not token:
            raise Unauthenticated("send a token in the Authorization header or ?access_token=")
        return service.authenticate(token)

    def current_actor(
        user: Annotated[User, Depends(current_user)],
        act_as: Annotated[
            int | None,
            Header(
                alias="X-Act-As",
                description="Demo servers only: the tester acts as this person id.",
            ),
        ] = None,
    ) -> Actor:
        if act_as is None:
            return Actor(user)
        # Belt and braces: the capability only exists in demo data, and is only
        # honoured when the server itself is running the demo.
        if not settings.demo:
            raise NotPermitted("acting as someone else is only possible on a demo server")
        return service.act_as(user, act_as)

    CurrentUser = Annotated[User, Depends(current_user)]
    CurrentActor = Annotated[Actor, Depends(current_actor)]

    # ------------------------------------------------------------------ pages

    @app.get("/", include_in_schema=False)
    def dashboard() -> HTMLResponse:
        page = files("rate_desk").joinpath("static/dashboard.html").read_text(encoding="utf-8")
        return HTMLResponse(page, headers={"Cache-Control": "no-cache"})

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, object]:
        return {"ok": True, "latest_event": service.latest_event_id()}

    # -------------------------------------------------------------- reference

    @app.get("/api/me", tags=["identity"], responses=ERRORS)
    def me(user: CurrentUser) -> MeOut:
        """Who your token says you are, and what you may do."""
        return MeOut.from_domain(user)

    @app.get("/api/people", tags=["identity"], responses=ERRORS)
    def people(user: CurrentUser) -> list[MeOut]:
        """Demo tester only: the people the tester can act as, and what each may do."""
        return [MeOut.from_domain(person) for person in service.people(caller=user)]

    @app.get("/api/projects", tags=["rates"], responses=ERRORS)
    def projects(_: CurrentUser) -> list[NamedOut]:
        return [NamedOut(code=p.code, name=p.name) for p in service.list_projects()]

    @app.get("/api/trades", tags=["rates"], responses=ERRORS)
    def trades(_: CurrentUser) -> list[NamedOut]:
        return [NamedOut(code=t.code, name=t.name) for t in service.list_trades()]

    @app.get("/api/projects/{project}/rates", tags=["rates"], responses=ERRORS)
    def rates_in_force(
        project: str,
        _: CurrentUser,
        as_of: Annotated[
            datetime | None,
            Query(description="A past moment, with a timezone. Defaults to now."),
        ] = None,
    ) -> ProjectRatesOut:
        """The rate in force for each trade on a project, now or at any moment in the past."""
        if as_of is not None and as_of.tzinfo is None:
            raise InvalidRequest("as_of needs a timezone, for example 2026-10-14T09:00:00Z")
        found, moment, rates = service.rates_in_force(project, as_of=as_of)
        return ProjectRatesOut(
            project=found.code,
            project_name=found.name,
            as_of=moment,
            rates=[RateOut.from_domain(rate) for rate in rates],
        )

    @app.get("/api/projects/{project}/trades/{trade}/history", tags=["rates"], responses=ERRORS)
    def rate_history(project: str, trade: str, _: CurrentUser) -> list[RateOut]:
        """Every rate this project and trade has had, oldest first, with the request that set it."""
        return [RateOut.from_domain(rate) for rate in service.rate_history(project, trade)]

    # --------------------------------------------------------------- requests

    @app.post("/api/requests", status_code=201, tags=["requests"], responses=ERRORS)
    def raise_request(body: RaiseRequestIn, actor: CurrentActor) -> RequestOut:
        """Ask for a rate to change. Nothing changes until somebody else approves it."""
        raised = service.raise_request(
            by=actor.user,
            proxy=actor.proxy,
            project=body.project,
            trade=body.trade,
            current=None if body.current_rate is None else Money.parse(body.current_rate),
            requested=Money.parse(body.requested_rate),
            reason=body.reason,
        )
        return RequestOut.from_domain(raised)

    @app.get("/api/requests", tags=["requests"], responses=ERRORS)
    def list_requests(
        user: CurrentUser,
        status: Literal["pending", "approved", "declined", "all"] = "pending",
        project: str | None = None,
        mine: Annotated[bool, Query(description="Only requests you raised.")] = False,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[RequestOut]:
        """Pending requests oldest first; decided ones newest first."""
        found = service.list_requests(
            status=None if status == "all" else Status(status),
            project=project,
            requested_by=user.id if mine else None,
            limit=limit,
        )
        return [RequestOut.from_domain(r) for r in found]

    @app.get("/api/requests/{request_id}", tags=["requests"], responses=ERRORS)
    def get_request(request_id: RequestId, _: CurrentUser) -> RequestOut:
        """One request. Once approved, ``outcome`` records what the approval did (Rule 2)."""
        return RequestOut.from_domain(service.get_request(request_id))

    @app.get(
        "/api/requests/{request_id}/explanation",
        tags=["requests"],
        response_class=PlainTextResponse,
        responses=ERRORS,
    )
    def explain_request(request_id: RequestId, _: CurrentUser) -> str:
        """The request in plain words, the same text the MCP tool gives."""
        return explain.explain(service.get_request(request_id))

    @app.post("/api/requests/{request_id}/approve", tags=["requests"], responses=ERRORS)
    def approve(request_id: RequestId, actor: CurrentActor) -> RequestOut:
        """Approve a request you did not raise. The new rate applies from this moment."""
        approved = service.approve(request_id, by=actor.user, via=Channel.API, proxy=actor.proxy)
        return RequestOut.from_domain(approved)

    @app.post("/api/requests/{request_id}/decline", tags=["requests"], responses=ERRORS)
    def decline(request_id: RequestId, body: DeclineIn, actor: CurrentActor) -> RequestOut:
        """Decline a request you did not raise. The requester sees your reason."""
        declined = service.decline(
            request_id, by=actor.user, reason=body.reason, via=Channel.API, proxy=actor.proxy
        )
        return RequestOut.from_domain(declined)

    # ------------------------------------------------------------ live stream

    @app.get("/api/events/stream", response_class=EventSourceResponse, tags=["live"])
    async def stream(
        _: Annotated[User, Depends(stream_user)],
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        since: Annotated[int | None, Query(description="Resume after this event id.")] = None,
    ) -> AsyncIterator[ServerSentEvent]:
        """Server-Sent Events for the wall dashboard.

        A new screen gets a ``snapshot`` of everything pending and recently
        decided, then each ``request.raised`` / ``request.approved`` /
        ``request.declined`` as it commits. A screen that reconnects with
        ``Last-Event-ID`` is sent exactly the events it missed.
        """
        # Subscribe before reading anything, so an event committed while we
        # read the snapshot is not lost; duplicates are skipped by id below.
        subscription = broadcaster.subscribe()
        try:
            cursor = _parse_event_id(last_event_id) if last_event_id is not None else since
            latest = await asyncio.to_thread(service.latest_event_id)
            if cursor is None or cursor > latest:
                # A new screen, or one that remembers a database that has since been reset.
                snap = await asyncio.to_thread(service.snapshot)
                yield ServerSentEvent(
                    event="snapshot",
                    id=str(snap.cursor),
                    retry=RETRY_MS,
                    data=SnapshotOut.from_domain(snap),
                )
                cursor = snap.cursor
            else:
                yield ServerSentEvent(comment="resuming", retry=RETRY_MS)
                while cursor < latest:
                    missed = await asyncio.to_thread(service.events_after, cursor)
                    if not missed:
                        break
                    for event in missed:
                        yield _sse(event)
                        cursor = event.id
            while True:
                try:
                    live = await asyncio.wait_for(subscription.next(), HEARTBEAT_SECONDS)
                except TimeoutError:
                    # No `id`, so the browser's Last-Event-ID is left alone.
                    yield ServerSentEvent(event="heartbeat", data={"cursor": cursor})
                    continue
                if live is None:  # shutting down, or this screen fell behind
                    break
                if live.id > cursor:
                    yield _sse(live)
                    cursor = live.id
        finally:
            subscription.close()

    # Mounted at the root, after every other route, so that POST /mcp is served
    # as-is. Mounted at /mcp, Starlette would answer /mcp with a redirect to
    # /mcp/, which not every MCP client follows on a POST.
    app.mount("/", mcp_app)
    app.add_middleware(AccessLogMiddleware)
    return app


def _parse_event_id(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


class AccessLogMiddleware:
    """One log line per request, without the query string.

    The dashboard has to put its token in the stream URL (browsers cannot set
    headers on EventSource), so uvicorn's own access log, which prints full
    URLs, is turned off in favour of this.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()

        async def logged(message: Message) -> None:
            if message["type"] == "http.response.start":
                log.info(
                    "%s %s %s %.0fms",
                    scope["method"],
                    scope["path"],
                    message["status"],
                    (time.perf_counter() - started) * 1000,
                )
            await send(message)

        await self.app(scope, receive, logged)
