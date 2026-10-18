"""The agent as an MCP host: an official SDK client speaking Streamable HTTP.

The client has no elicitation callback. Every call that may ask for input uses
`allow_input_required=True`, so the `InputRequiredResult` reaches the agent raw,
and the agent decides what to do with it (pause the A2A Task). The `requestState`
is treated as opaque bytes: stored and echoed, never opened.
"""

from __future__ import annotations

import secrets
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import mcp_types as types
from mcp import Client
from mcp.client.session import ClientSession

PROTOCOL = "2026-07-28"
POLICY_URI = "politica://uso"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class _FormElicitationSession(ClientSession):
    """Declares only `{"elicitation": {"form": {}}}` in the `_meta` of every request.

    The SDK's `ClientSession` only advertises elicitation when given a callback, and
    then it advertises form and url and answers the questions by itself. The agent
    wants the opposite: advertise form mode and never answer on its own.
    """

    def _build_capabilities(self, version: str) -> types.ClientCapabilities:
        return types.ClientCapabilities(elicitation=types.ElicitationCapability(form=types.FormElicitationCapability()))


class _Client(Client):
    async def _build_session(self, exit_stack: AsyncExitStack) -> ClientSession:
        session = await super()._build_session(exit_stack)
        session.__class__ = _FormElicitationSession
        return session


def new_traceparent(trace_id: str, flags: str = "01") -> str:
    """Same trace-id as the A2A client, new span-id for every MCP request."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-{flags}"


@dataclass
class Question:
    """An `input_required` as the agent sees it: what to ask and what to keep."""

    key: str
    field: str
    options: list[str]
    request_state: str


class MCPHost:
    def __init__(self, url: str) -> None:
        self.url = url
        self._stack = AsyncExitStack()
        self._client: Client | None = None

    async def open(self) -> None:
        # Pinned version: no server/discover and no initialize. Every request carries
        # version and capabilities in its own _meta; nothing is inferred from the connection.
        client = _Client(
            self.url,
            mode=PROTOCOL,
            client_info=types.Implementation(name="agente-central-de-salas", version="1.0.0"),
            cache=None,
        )
        self._client = await self._stack.enter_async_context(client)

    async def close(self) -> None:
        await self._stack.aclose()

    @property
    def _session(self) -> ClientSession:
        assert self._client is not None, "MCPHost.open() was not called"
        return self._client.session

    @staticmethod
    def _meta(trace: tuple[str, str]) -> dict[str, Any]:
        return {"traceparent": new_traceparent(*trace)}

    async def list_tools(self, trace: tuple[str, str]) -> dict[str, types.Tool]:
        result = await self._session.list_tools(params=types.PaginatedRequestParams(_meta=self._meta(trace)))
        return {t.name: t for t in result.tools}

    async def policy_version(self, trace: tuple[str, str]) -> str:
        result = await self._session.read_resource(POLICY_URI, meta=self._meta(trace))
        text = next((c.text for c in result.contents if isinstance(c, types.TextResourceContents)), "")
        first_line = text.splitlines()[0] if text else ""
        key, _, value = first_line.partition(":")
        if key.strip() != "versao" or not value.strip():
            raise RuntimeError(f"resource {POLICY_URI} does not declare the version on its first line")
        return value.strip()

    async def call(
        self,
        tool: str,
        arguments: dict[str, Any],
        trace: tuple[str, str],
        *,
        input_responses: types.InputResponses | None = None,
        request_state: str | None = None,
    ) -> types.CallToolResult | types.InputRequiredResult:
        """One tools/call. The SDK mints a new JSON-RPC id on every call, including the retry."""
        return await self._session.call_tool(
            tool,
            arguments,
            input_responses=input_responses,
            request_state=request_state,
            meta=self._meta(trace),
            allow_input_required=True,
        )


def question_from(result: types.InputRequiredResult) -> Question:
    """Extracts the form-mode elicitation from an `input_required`, without touching the requestState."""
    requests = result.input_requests or {}
    if len(requests) != 1 or not result.request_state:
        raise RuntimeError(f"unexpected input_required: {len(requests)} requests")
    key, request = next(iter(requests.items()))
    if not isinstance(request, types.ElicitRequest) or not isinstance(request.params, types.ElicitRequestFormParams):
        raise RuntimeError("input_required without a form-mode elicitation")
    fields = (request.params.requested_schema or {}).get("properties") or {}
    if len(fields) != 1:
        raise RuntimeError("the elicitation is not a flat one-field schema")
    name, spec = next(iter(fields.items()))
    options = list(spec.get("enum") or ([spec["const"]] if "const" in spec else []))
    return Question(key=key, field=name, options=options, request_state=result.request_state)


def text_of(result: types.CallToolResult) -> str:
    return " ".join(c.text for c in result.content if isinstance(c, types.TextContent)).strip()
