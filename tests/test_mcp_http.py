"""MCP over Streamable HTTP, the way a remote Claude connects to the hosted server."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ElicitRequestParams, ElicitResult, TextContent

from rate_desk.domain import Channel, Status
from tests.conftest import LiveServer

pytestmark = pytest.mark.anyio


@contextlib.asynccontextmanager
async def remote(live: LiveServer, token: str, **kwargs: Any) -> AsyncIterator[Client]:
    headers = {"Authorization": f"Bearer {token}"}
    # follow_redirects=False: the endpoint must answer at /mcp itself.
    async with httpx2.AsyncClient(headers=headers, timeout=10, follow_redirects=False) as http:
        transport = streamable_http_client(f"{live.url}/mcp", http_client=http)
        async with Client(transport, **kwargs) as client:
            yield client


def test_no_token_no_mcp(live_server: LiveServer) -> None:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with httpx.Client(base_url=live_server.url) as http:
        assert http.post("/mcp", headers=headers, content=b"{}").status_code == 401
        bad = headers | {"Authorization": "Bearer nope"}
        assert http.post("/mcp", headers=bad, content=b"{}").status_code == 401


def test_a_foreign_host_header_is_refused(live_server: LiveServer) -> None:
    """DNS-rebinding protection: only the configured public host and localhost are served."""
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": "Bearer t-tom",
        "Host": "evil.example",
    }
    with httpx.Client(base_url=live_server.url) as http:
        assert http.post("/mcp", headers=headers, content=b"{}").status_code == 421


async def test_tools_work_with_a_token(live_server: LiveServer) -> None:
    live_server.world.raise_ganger()
    async with remote(live_server, "t-wall") as client:
        result = await client.call_tool("list_pending_requests", {})
    assert result.structured_content is not None
    assert result.structured_content["count"] == 1


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_approval_over_http_is_made_as_the_tokens_owner(
    live_server: LiveServer, mode: str
) -> None:
    world = live_server.world
    request = world.raise_ganger()
    shown: list[str] = []

    async def human(context: Any, params: ElicitRequestParams) -> ElicitResult:
        shown.append(params.message)
        return ElicitResult(action="accept", content={"approve": True})

    async with remote(live_server, "t-tom", mode=mode, elicitation_callback=human) as client:
        result = await client.call_tool("approve_request", {"request_id": request.id})

    first = result.content[0]
    assert isinstance(first, TextContent)
    assert not result.is_error, first.text
    assert len(shown) == 1
    assert shown[0].startswith(f"Approve request #{request.id} as Tom?")
    approved = world.service.get_request(request.id)
    assert approved.status is Status.APPROVED
    assert approved.decided_by is not None
    assert approved.decided_by.name == "Tom"
    assert approved.decided_via is Channel.MCP


async def test_a_requesters_token_cannot_approve_over_http(live_server: LiveServer) -> None:
    request = live_server.world.raise_ganger(by=live_server.world.sam)

    async def human(context: Any, params: ElicitRequestParams) -> ElicitResult:
        return ElicitResult(action="accept", content={"approve": True})

    async with remote(live_server, "t-dave", elicitation_callback=human) as client:
        result = await client.call_tool("approve_request", {"request_id": request.id})
    assert result.is_error
    assert live_server.world.service.get_request(request.id).status is Status.PENDING
