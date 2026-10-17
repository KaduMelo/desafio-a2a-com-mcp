"""Agente A2A da Central de Salas: servidor A2A v1.0 (JSON-RPC) por fora, host MCP por dentro.

Sem LLM: o pedido chega em formato fixo e o agente decide por regra. O agente
nao implementa regra de sala; conflito, politica e alternativas sao do servidor
MCP. O que ele faz e traduzir protocolo: Task A2A <-> tools/call MCP, e
`input_required` (MRTR) <-> TASK_STATE_INPUT_REQUIRED.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import mcp_types as types
import uvicorn
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from host_mcp import HostMCP, Pergunta, log, pergunta_de, texto_de

HOST = os.environ.get("AGENTE_HOST", "127.0.0.1")
PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://{HOST}:{PORTA}").rstrip("/")
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

TOOL_RESERVA = "reservar_sala"
FORMATO_PEDIDO = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
REJECTED = "TASK_STATE_REJECTED"
TERMINAIS = {COMPLETED, CANCELED, FAILED, REJECTED}

# Erros JSON-RPC do A2A v1.0
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004


# --- cartao --------------------------------------------------------------------

AGENT_CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [{"url": f"{URL_PUBLICA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}


# --- Tasks ---------------------------------------------------------------------


@dataclass
class Pausa:
    """O que a Task guarda enquanto espera o cliente A2A. Nunca sai do agente."""

    tool: str
    argumentos: dict[str, Any]
    pergunta: Pergunta


@dataclass
class Task:
    id: str
    context_id: str
    estado: str = SUBMITTED
    mensagem: dict[str, Any] | None = None
    historico: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    trace_id: str = ""
    trace_flags: str = "01"
    politica: str | None = None
    pausa: Pausa | None = None
    trava: asyncio.Lock = field(default_factory=asyncio.Lock)

    def para_json(self) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.estado}
        if self.mensagem is not None:
            status["message"] = self.mensagem
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": self.historico,
            "artifacts": self.artifacts,
        }

    def transitar(self, estado: str, texto: str | None = None) -> None:
        if self.estado in TERMINAIS:
            raise RuntimeError(f"a Task {self.id} ja terminou em {self.estado}")
        anterior, self.estado = self.estado, estado
        self.mensagem = None
        if texto is not None:
            self.mensagem = {
                "messageId": f"msg-{secrets.token_hex(6)}",
                "role": "ROLE_AGENT",
                "parts": [{"text": texto}],
                "taskId": self.id,
                "contextId": self.context_id,
            }
            self.historico.append(self.mensagem)
        log(f"[a2a] task={self.id} {anterior} -> {estado}" + (f" ({texto})" if texto else ""))


TASKS: dict[str, Task] = {}
host = HostMCP(MCP_URL)


class ErroRPC(Exception):
    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


# --- leitura do pedido em formato fixo -----------------------------------------

_PEDIDO = re.compile(r"^\s*reservar\s+(?P<campos>.+?)\s*$", re.IGNORECASE)
_CAMPO = re.compile(r"(\w+)=(\S+)")
_ESCOLHA = re.compile(r"^\s*escolha=(?P<valor>\S+)\s*$")


def ler_pedido(texto: str) -> dict[str, str] | None:
    casado = _PEDIDO.match(texto)
    if not casado:
        return None
    campos = dict(_CAMPO.findall(casado.group("campos")))
    return campos if {"sala", "inicio", "fim", "responsavel"} <= campos.keys() else None


def ler_escolha(texto: str) -> str | None:
    casado = _ESCOLHA.match(texto)
    return casado.group("valor") if casado else None


def texto_da_mensagem(mensagem: dict[str, Any]) -> str:
    return " ".join(p.get("text", "") for p in mensagem.get("parts") or [] if isinstance(p, dict)).strip()


def trace_do_header(valor: str | None) -> tuple[str, str] | None:
    """Le o trace-id e as flags de um traceparent W3C valido."""
    casado = re.fullmatch(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})", (valor or "").strip())
    if not casado or casado.group(1) == "0" * 32:
        return None
    return casado.group(1), casado.group(3)


# --- a ponte -------------------------------------------------------------------


def alternativas(pergunta: Pergunta) -> str:
    return "alternativas: " + ", ".join(pergunta.opcoes)


async def concluir_com(task: Task, resultado: types.CallToolResult | types.InputRequiredResult, tool: str,
                       argumentos: dict[str, Any]) -> None:
    """Traduz o resultado de um tools/call MCP para o estado da Task A2A."""
    if isinstance(resultado, types.InputRequiredResult):
        # PONTE (ida): input_required do MCP vira TASK_STATE_INPUT_REQUIRED. O
        # requestState fica guardado na Task, opaco, e so volta para o servidor MCP.
        pergunta = pergunta_de(resultado)
        task.pausa = Pausa(tool=tool, argumentos=argumentos, pergunta=pergunta)
        task.transitar(INPUT_REQUIRED, alternativas(pergunta))
        return

    task.pausa = None
    if resultado.is_error:
        task.transitar(FAILED, texto_de(resultado) or "a tool devolveu erro")
        return
    dados = resultado.structured_content or {}
    if dados.get("reservado") is False:
        task.transitar(CANCELED, f"Reserva nao realizada: {dados.get('motivo') or 'recusada'}.")
        return
    reserva = {
        "reserva": dados.get("reserva"),
        "sala": dados.get("sala"),
        "inicio": dados.get("inicio"),
        "fim": dados.get("fim"),
        "responsavel": dados.get("responsavel"),
        "politica": task.politica,
    }
    task.artifacts.append(
        {"artifactId": f"art-{secrets.token_hex(6)}", "name": "reserva", "parts": [{"text": json.dumps(reserva)}]}
    )
    task.transitar(COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")


async def iniciar(task: Task, texto: str) -> None:
    pedido = ler_pedido(texto)
    if pedido is None:
        task.transitar(REJECTED, f"Pedido fora do formato. Use: {FORMATO_PEDIDO}")
        return
    task.transitar(WORKING)
    trace = (task.trace_id, task.trace_flags)
    tools = await host.listar_tools(trace)
    tool = tools.get(TOOL_RESERVA)
    if tool is None:
        task.transitar(FAILED, f"O servidor MCP nao oferece a tool {TOOL_RESERVA}")
        return
    task.politica = await host.versao_da_politica(trace)
    # Os argumentos saem do inputSchema descoberto, nao de uma lista fixa no codigo.
    esperados = list((tool.input_schema or {}).get("properties", {}))
    faltando = [nome for nome in (tool.input_schema or {}).get("required", []) if nome not in pedido]
    if faltando:
        task.transitar(REJECTED, f"Pedido sem {', '.join(faltando)}. Use: {FORMATO_PEDIDO}")
        return
    argumentos = {nome: pedido[nome] for nome in esperados if nome in pedido}
    resultado = await host.chamar(TOOL_RESERVA, argumentos, trace)
    await concluir_com(task, resultado, TOOL_RESERVA, argumentos)


async def retomar(task: Task, texto: str) -> None:
    pausa = task.pausa
    assert pausa is not None
    escolha = ler_escolha(texto)
    if escolha == "recusar":
        resposta = types.ElicitResult(action="decline")
    elif escolha in pausa.pergunta.opcoes:
        resposta = types.ElicitResult(action="accept", content={pausa.pergunta.campo: escolha})
    else:
        # Escolha fora do enum: a Task continua pausada e repete a pergunta.
        task.transitar(INPUT_REQUIRED, alternativas(pausa.pergunta))
        return
    task.transitar(WORKING)
    # PONTE (volta): o mesmo tools/call, com id JSON-RPC novo, levando a resposta
    # sob a mesma chave do inputRequests e o requestState ecoado sem modificacao.
    resultado = await host.chamar(
        pausa.tool,
        pausa.argumentos,
        (task.trace_id, task.trace_flags),
        input_responses={pausa.pergunta.chave: resposta},
        request_state=pausa.pergunta.request_state,
    )
    await concluir_com(task, resultado, pausa.tool, pausa.argumentos)


async def processar(task: Task, fazer) -> None:
    try:
        await fazer
    except MCPError as erro:
        task.pausa = None
        task.transitar(FAILED, f"Erro do servidor MCP ({erro.error.code}): {erro.error.message}")
    except Exception as erro:  # o cliente A2A sempre recebe uma Task coerente
        log(f"[a2a] task={task.id} erro inesperado: {erro!r}")
        task.pausa = None
        if task.estado not in TERMINAIS:
            task.transitar(FAILED, f"Erro interno do agente: {erro}")


# --- JSON-RPC --------------------------------------------------------------------


async def send_message(params: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
    mensagem = params.get("message")
    if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list):
        raise ErroRPC(-32602, "params.message com parts e obrigatorio")
    texto = texto_da_mensagem(mensagem)
    trace = trace_do_header(traceparent)
    task_id = mensagem.get("taskId")

    if task_id:
        task = TASKS.get(task_id)
        if task is None:
            raise ErroRPC(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
        async with task.trava:
            if task.estado in TERMINAIS:
                raise ErroRPC(UNSUPPORTED_OPERATION, f"A Task {task_id} ja terminou em {task.estado}")
            if task.estado != INPUT_REQUIRED or task.pausa is None:
                raise ErroRPC(UNSUPPORTED_OPERATION, f"A Task {task_id} nao esta esperando input")
            if trace:
                task.trace_id, task.trace_flags = trace
            task.historico.append(mensagem)
            await processar(task, retomar(task, texto))
            return {"task": task.para_json()}

    task = Task(
        id=f"task-{secrets.token_hex(6)}",
        context_id=mensagem.get("contextId") or f"ctx-{secrets.token_hex(6)}",
    )
    task.trace_id, task.trace_flags = trace or (secrets.token_hex(16), "01")
    task.historico.append(mensagem)
    TASKS[task.id] = task
    log(f"[a2a] task={task.id} criada em {SUBMITTED} trace-id={task.trace_id}")
    async with task.trava:
        await processar(task, iniciar(task, texto))
    return {"task": task.para_json()}


async def get_task(params: dict[str, Any], _traceparent: str | None) -> dict[str, Any]:
    task = TASKS.get(str(params.get("id", "")))
    if task is None:
        raise ErroRPC(TASK_NOT_FOUND, f"Task nao encontrada: {params.get('id')}")
    return {"task": task.para_json()}


METODOS = {"SendMessage": send_message, "GetTask": get_task}


def resposta_rpc(id_: Any, *, resultado: Any = None, erro: tuple[int, str] | None = None) -> JSONResponse:
    corpo: dict[str, Any] = {"jsonrpc": "2.0", "id": id_}
    if erro is not None:
        corpo["error"] = {"code": erro[0], "message": erro[1]}
    else:
        corpo["result"] = resultado
    return JSONResponse(corpo)


async def endpoint_a2a(request: Request) -> JSONResponse:
    try:
        corpo = json.loads(await request.body())
    except ValueError:
        return resposta_rpc(None, erro=(-32700, "JSON invalido"))
    if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
        return resposta_rpc(corpo.get("id") if isinstance(corpo, dict) else None, erro=(-32600, "Request invalido"))
    id_, metodo = corpo.get("id"), corpo["method"]
    params = corpo.get("params") if isinstance(corpo.get("params"), dict) else {}
    traceparent = request.headers.get("traceparent")
    log(f"[a2a] method={metodo} id={id_} traceparent={traceparent or '-'}")
    tratador = METODOS.get(metodo)
    if tratador is None:
        return resposta_rpc(id_, erro=(-32601, f"Metodo nao suportado: {metodo}"))
    try:
        return resposta_rpc(id_, resultado=await tratador(params, traceparent))
    except ErroRPC as erro:
        return resposta_rpc(id_, erro=(erro.codigo, erro.mensagem))


async def agent_card(_request: Request) -> JSONResponse:
    return JSONResponse(AGENT_CARD)


@asynccontextmanager
async def ciclo_de_vida(_app: Starlette):
    await host.abrir()
    log(f"[a2a] central-de-salas em {URL_PUBLICA}/a2a, host MCP de {MCP_URL}")
    try:
        yield
    finally:
        await host.fechar()


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/a2a", endpoint_a2a, methods=["POST"]),
    ],
    lifespan=ciclo_de_vida,
)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORTA, log_level="warning")
