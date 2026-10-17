"""O agente como host MCP: um cliente do SDK oficial falando Streamable HTTP.

O cliente nao tem callback de elicitation. Toda chamada que pode pedir input usa
`allow_input_required=True`, entao o `InputRequiredResult` chega cru ao agente,
que decide o que fazer com ele (pausar a Task A2A). O `requestState` e tratado
como bytes opacos: guardado e ecoado, nunca aberto.
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
    """Declara so `{"elicitation": {"form": {}}}` no `_meta` de todo request.

    O `ClientSession` do SDK so anuncia elicitation quando recebe um callback, e
    nesse caso anuncia form e url e responde as perguntas sozinho. O agente quer o
    contrario: anunciar form mode e nunca responder por conta propria.
    """

    def _build_capabilities(self, version: str) -> types.ClientCapabilities:
        return types.ClientCapabilities(elicitation=types.ElicitationCapability(form=types.FormElicitationCapability()))


class _Cliente(Client):
    async def _build_session(self, exit_stack: AsyncExitStack) -> ClientSession:
        sessao = await super()._build_session(exit_stack)
        sessao.__class__ = _SessaoComElicitationForm
        return sessao


def novo_traceparent(trace_id: str, flags: str = "01") -> str:
    """Mesmo trace-id do cliente A2A, span-id novo para cada request MCP."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-{flags}"


@dataclass
class Pergunta:
    """Um `input_required` visto pelo agente: o que perguntar e o que guardar."""

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
        # Versao fixada: sem server/discover e sem initialize. Cada request leva
        # versao e capabilities no proprio _meta; nada e inferido de conexao.
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
        assert self._cliente is not None, "HostMCP.abrir() nao foi chamado"
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
            raise RuntimeError(f"o resource {POLITICA_URI} nao declara a versao na primeira linha")
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
        """Um tools/call. O SDK cunha um id JSON-RPC novo a cada chamada, inclusive no retry."""
        return await self._sessao.call_tool(
            tool,
            argumentos,
            input_responses=input_responses,
            request_state=request_state,
            meta=self._meta(trace),
            allow_input_required=True,
        )


def pergunta_de(resultado: types.InputRequiredResult) -> Pergunta:
    """Extrai a elicitation em form mode de um `input_required`, sem tocar no requestState."""
    pedidos = resultado.input_requests or {}
    if len(pedidos) != 1 or not resultado.request_state:
        raise RuntimeError(f"input_required inesperado: {len(pedidos)} pedidos")
    chave, pedido = next(iter(pedidos.items()))
    if not isinstance(pedido, types.ElicitRequest) or not isinstance(pedido.params, types.ElicitRequestFormParams):
        raise RuntimeError("input_required sem elicitation em form mode")
    campos = (pedido.params.requested_schema or {}).get("properties") or {}
    if len(campos) != 1:
        raise RuntimeError("a elicitation nao e um schema plano de um campo")
    nome, campo = next(iter(campos.items()))
    opcoes = list(campo.get("enum") or ([campo["const"]] if "const" in campo else []))
    return Pergunta(chave=chave, campo=nome, opcoes=opcoes, request_state=resultado.request_state)


def texto_de(resultado: types.CallToolResult) -> str:
    return " ".join(c.text for c in resultado.content if isinstance(c, types.TextContent)).strip()
