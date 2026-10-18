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

PROTOCOLO = "2026-07-28"
POLITICA_URI = "politica://uso"


def log(mensagem: str) -> None:
    print(mensagem, file=sys.stderr, flush=True)


class _SessaoComElicitationForm(ClientSession):
    """Declares only `{"elicitation": {"form": {}}}` in the `_meta` of every request.

    The SDK's `ClientSession` only advertises elicitation when given a callback, and
    then it advertises form and url and answers the questions by itself. The agent
    wants the opposite: advertise form mode and never answer on its own.
    """

    def _build_capabilities(self, version: str) -> types.ClientCapabilities:
        return types.ClientCapabilities(elicitation=types.ElicitationCapability(form=types.FormElicitationCapability()))


class _Cliente(Client):
    async def _build_session(self, exit_stack: AsyncExitStack) -> ClientSession:
        sessao = await super()._build_session(exit_stack)
        sessao.__class__ = _SessaoComElicitationForm
        return sessao


def novo_traceparent(trace_id: str, flags: str = "01") -> str:
    """Same trace-id as the A2A client, new span-id for every MCP request."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-{flags}"


@dataclass
class Pergunta:
    """An `input_required` as the agent sees it: what to ask and what to keep."""

    chave: str
    campo: str
    opcoes: list[str]
    request_state: str


class HostMCP:
    def __init__(self, url: str) -> None:
        self.url = url
        self._pilha = AsyncExitStack()
        self._cliente: Client | None = None

    async def abrir(self) -> None:
        # Pinned version: no server/discover and no initialize. Every request carries
        # version and capabilities in its own _meta; nothing is inferred from the connection.
        cliente = _Cliente(
            self.url,
            mode=PROTOCOLO,
            client_info=types.Implementation(name="agente-central-de-salas", version="1.0.0"),
            cache=None,
        )
        self._cliente = await self._pilha.enter_async_context(cliente)

    async def fechar(self) -> None:
        await self._pilha.aclose()

    @property
    def _sessao(self) -> ClientSession:
        assert self._cliente is not None, "HostMCP.abrir() was not called"
        return self._cliente.session

    @staticmethod
    def _meta(trace: tuple[str, str]) -> dict[str, Any]:
        return {"traceparent": novo_traceparent(*trace)}

    async def listar_tools(self, trace: tuple[str, str]) -> dict[str, types.Tool]:
        resultado = await self._sessao.list_tools(params=types.PaginatedRequestParams(_meta=self._meta(trace)))
        return {t.name: t for t in resultado.tools}

    async def versao_da_politica(self, trace: tuple[str, str]) -> str:
        resultado = await self._sessao.read_resource(POLITICA_URI, meta=self._meta(trace))
        texto = next((c.text for c in resultado.contents if isinstance(c, types.TextResourceContents)), "")
        primeira = texto.splitlines()[0] if texto else ""
        chave, _, valor = primeira.partition(":")
        if chave.strip() != "versao" or not valor.strip():
            raise RuntimeError(f"resource {POLITICA_URI} does not declare the version on its first line")
        return valor.strip()

    async def chamar(
        self,
        tool: str,
        argumentos: dict[str, Any],
        trace: tuple[str, str],
        *,
        input_responses: types.InputResponses | None = None,
        request_state: str | None = None,
    ) -> types.CallToolResult | types.InputRequiredResult:
        """One tools/call. The SDK mints a new JSON-RPC id on every call, including the retry."""
        return await self._sessao.call_tool(
            tool,
            argumentos,
            input_responses=input_responses,
            request_state=request_state,
            meta=self._meta(trace),
            allow_input_required=True,
        )


def pergunta_de(resultado: types.InputRequiredResult) -> Pergunta:
    """Extracts the form-mode elicitation from an `input_required`, without touching the requestState."""
    pedidos = resultado.input_requests or {}
    if len(pedidos) != 1 or not resultado.request_state:
        raise RuntimeError(f"unexpected input_required: {len(pedidos)} requests")
    chave, pedido = next(iter(pedidos.items()))
    if not isinstance(pedido, types.ElicitRequest) or not isinstance(pedido.params, types.ElicitRequestFormParams):
        raise RuntimeError("input_required without a form-mode elicitation")
    campos = (pedido.params.requested_schema or {}).get("properties") or {}
    if len(campos) != 1:
        raise RuntimeError("the elicitation is not a flat one-field schema")
    nome, campo = next(iter(campos.items()))
    opcoes = list(campo.get("enum") or ([campo["const"]] if "const" in campo else []))
    return Pergunta(chave=chave, campo=nome, opcoes=opcoes, request_state=resultado.request_state)


def texto_de(resultado: types.CallToolResult) -> str:
    return " ".join(c.text for c in resultado.content if isinstance(c, types.TextContent)).strip()
