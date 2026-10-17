"""Servidor MCP da Central de Salas (Streamable HTTP, revisao 2026-07-28).

Expoe tres tools (listar_salas, consultar_disponibilidade, reservar_sala) e o
resource politica://uso. Quando a reserva conflita, reservar_sala responde
input_required pelo MRTR do SDK: o resolver `escolha_de_sala` devolve um
`Elicit`, o SDK empacota a pergunta em `inputRequests` e sela o `requestState`
com AES-256-GCM (chave de REQUEST_STATE_SECRET). Nada fica em memoria entre o
input_required e o retry.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import uvicorn
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.server.request_state import RequestStateSecurity
from pydantic import BaseModel, Field, create_model

RAIZ = Path(__file__).resolve().parent.parent
DADOS = Path(os.environ.get("DADOS_DIR", RAIZ / "dados"))
SAO_PAULO = timezone(timedelta(hours=-3))
JANELA_INICIO, JANELA_FIM = 8 * 60, 20 * 60
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3
VALIDADE_REQUEST_STATE = 10 * 60  # segundos

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


def log(mensagem: str) -> None:
    print(mensagem, file=sys.stderr, flush=True)


# --- dominio: dados do starter, reservas em memoria -------------------------

SALAS: list[dict[str, Any]] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
SALAS_POR_ID = {s["id"]: s for s in SALAS}
RESERVAS: list[dict[str, Any]] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
TEXTO_POLITICA = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
VERSAO_POLITICA = TEXTO_POLITICA.splitlines()[0].split(":", 1)[1].strip()


def _instante(valor: str) -> datetime:
    try:
        instante = datetime.fromisoformat(valor)
    except ValueError:
        raise ToolError(f"Data invalida: {valor}") from None
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=SAO_PAULO)
    return instante.astimezone(SAO_PAULO)


def _minutos(instante: datetime) -> int:
    return instante.hour * 60 + instante.minute + (1 if instante.second or instante.microsecond else 0)


def _na_janela(instante: datetime) -> bool:
    return JANELA_INICIO <= _minutos(instante) <= JANELA_FIM


def validar_pedido(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Regras da politica de uso, na ordem: sala, intervalo, janela, duracao."""
    if sala not in SALAS_POR_ID:
        raise ToolError(f"Sala inexistente: {sala}")
    ini, fi = _instante(inicio), _instante(fim)
    if fi <= ini:
        raise ToolError(ERRO_INTERVALO)
    if ini.date() != fi.date() or not _na_janela(ini) or not _na_janela(fi):
        raise ToolError(ERRO_JANELA)
    if fi - ini > DURACAO_MAXIMA:
        raise ToolError(ERRO_DURACAO)
    return ini, fi


def conflitos(sala: str, ini: datetime, fi: datetime) -> list[dict[str, Any]]:
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala and _instante(r["inicio"]) < fi and ini < _instante(r["fim"])
    ]


def alternativas(sala: str, ini: datetime, fi: datetime) -> list[str]:
    """Salas livres com capacidade >= a pedida, por capacidade e id, no maximo tres."""
    minima = SALAS_POR_ID[sala]["capacidade"]
    livres = [
        s for s in SALAS if s["id"] != sala and s["capacidade"] >= minima and not conflitos(s["id"], ini, fi)
    ]
    livres.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in livres[:MAX_ALTERNATIVAS]]


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> dict[str, Any]:
    reserva = {
        "id": f"res-{len(RESERVAS) + 1:04d}",
        "sala": sala,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
    }
    RESERVAS.append(reserva)
    return reserva


# --- contratos de saida -----------------------------------------------------


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class EscolhaDeSala(BaseModel):
    sala: str


def schema_da_escolha(opcoes: list[str]) -> type[EscolhaDeSala]:
    """Schema plano da elicitation: `sala` restrita as alternativas, na ordem calculada."""
    return create_model(
        "EscolhaDeSala",
        __base__=EscolhaDeSala,
        sala=(Literal[tuple(opcoes)], Field(title="Sala", description="Sala alternativa escolhida")),
    )


# --- servidor MCP -----------------------------------------------------------


def _segredo() -> str:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        sys.exit(
            "REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. Gere com:\n"
            '  export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")'
        )
    return segredo


mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[_segredo()], ttl=VALIDADE_REQUEST_STATE),
)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    ini, fi = validar_pedido(sala, inicio, fim)
    em_conflito = [ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in conflitos(sala, ini, fi)]
    return Disponibilidade(sala=sala, livre=not em_conflito, conflitos=em_conflito)


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[EscolhaDeSala] | None:
    """Resolver do MRTR: so pergunta quando a sala pedida esta ocupada.

    Roda de novo a cada round (inclusive depois de um restart); a resposta do
    cliente so e aceita se a pergunta renderizada for identica a que foi selada.
    """
    ini, fi = validar_pedido(sala, inicio, fim)
    if not conflitos(sala, ini, fi):
        return None
    opcoes = alternativas(sala, ini, fi)
    if not opcoes:
        raise ToolError(ERRO_SEM_ALTERNATIVA)
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", schema_da_escolha(opcoes))


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[EscolhaDeSala], Resolve(escolha_de_sala)],
) -> ReservaOut:
    if not isinstance(escolha, AcceptedElicitation):
        motivo = "recusado" if escolha.action == "decline" else "cancelado"
        return ReservaOut(reservado=False, motivo=motivo)
    destino = escolha.data.sala if escolha.data is not None else sala
    ini, fi = validar_pedido(destino, inicio, fim)
    if conflitos(destino, ini, fi):
        raise ToolError(ERRO_SEM_ALTERNATIVA)
    reserva = criar_reserva(destino, inicio, fim, responsavel)
    return ReservaOut(
        reserva=reserva["id"],
        sala=destino,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=VERSAO_POLITICA,
    )


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown",
              description="Politica de uso das salas de reuniao.")
def politica_de_uso() -> str:
    return TEXTO_POLITICA


# --- log de cada request no stderr -----------------------------------------


class LogDeRequests:
    """ASGI: registra metodo, id, nome e traceparent de todo POST recebido, antes do SDK."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        partes: list[bytes] = []
        while True:
            mensagem = await receive()
            partes.append(mensagem.get("body", b""))
            if not mensagem.get("more_body"):
                break
        corpo = b"".join(partes)
        self._registrar(corpo)
        entregue = False

        async def reenviar() -> dict:
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        await self.app(scope, reenviar, send)

    @staticmethod
    def _registrar(corpo: bytes) -> None:
        try:
            msg = json.loads(corpo)
        except ValueError:
            log("[mcp] request com corpo invalido")
            return
        if not isinstance(msg, dict):
            log("[mcp] request fora do formato JSON-RPC")
            return
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        extra = ""
        if params.get("name") or params.get("uri"):
            extra += f" name={params.get('name') or params.get('uri')}"
        if "requestState" in params:
            extra += " retry=sim"
        capacidades = meta.get("io.modelcontextprotocol/clientCapabilities")
        if isinstance(capacidades, dict):
            elicitation = capacidades.get("elicitation")
            extra += f" elicitation={'+'.join(sorted(elicitation)) or 'vazia' if isinstance(elicitation, dict) else 'nao'}"
        log(
            f"[mcp] method={msg.get('method')} id={msg.get('id')}{extra} "
            f"traceparent={meta.get('traceparent', '-')}"
        )


app = LogDeRequests(mcp.streamable_http_app(json_response=True, stateless_http=True))


def main() -> None:
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    porta = int(os.environ.get("MCP_PORT", "7301"))
    log(f"[mcp] central-de-salas em http://{host}:{porta}/mcp (politica {VERSAO_POLITICA})")
    uvicorn.run(app, host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
