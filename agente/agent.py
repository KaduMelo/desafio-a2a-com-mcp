"""Central de Salas A2A agent: A2A v1.0 server (JSON-RPC) outside, MCP host inside.

No LLM: the request arrives in a fixed format and the agent decides by rule. The
agent implements no room rules; conflicts, policy and alternatives belong to the
MCP server. What it does is translate protocol: A2A Task <-> MCP tools/call, and
`input_required` (MRTR) <-> TASK_STATE_INPUT_REQUIRED.

The request formats (`reservar sala=...`, `escolha=...`), the `alternativas: `
line and the skill id are fixed by the challenge contract and checked by the
validator, so they stay in Portuguese.
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

from mcp_host import MCPHost, Question, log, question_from, text_of

HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("AGENT_PORT", "7300"))
PUBLIC_URL = os.environ.get("AGENT_URL", f"http://{HOST}:{PORT}").rstrip("/")
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

BOOKING_TOOL = "reservar_sala"
REQUEST_FORMAT = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
REJECTED = "TASK_STATE_REJECTED"
TERMINAL = {COMPLETED, CANCELED, FAILED, REJECTED}

# A2A v1.0 JSON-RPC errors
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004


# --- agent card ----------------------------------------------------------------

AGENT_CARD = {
    "name": "Central de Salas",
    "description": "Books Hill Valley Tech meeting rooms.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [{"url": f"{PUBLIC_URL}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Book a room",
            "description": "Books a room for an interval. If there is a conflict, asks which alternative to use.",
            "tags": ["rooms", "calendar"],
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
class Pause:
    """What the Task keeps while waiting for the A2A client. It never leaves the agent."""

    tool: str
    arguments: dict[str, Any]
    question: Question


@dataclass
class Task:
    id: str
    context_id: str
    state: str = SUBMITTED
    message: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    trace_id: str = ""
    trace_flags: str = "01"
    policy: str | None = None
    pause: Pause | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def to_json(self) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.state}
        if self.message is not None:
            status["message"] = self.message
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": self.history,
            "artifacts": self.artifacts,
        }

    def transition(self, state: str, text: str | None = None) -> None:
        if self.state in TERMINAL:
            raise RuntimeError(f"Task {self.id} already ended in {self.state}")
        previous, self.state = self.state, state
        self.message = None
        if text is not None:
            self.message = {
                "messageId": f"msg-{secrets.token_hex(6)}",
                "role": "ROLE_AGENT",
                "parts": [{"text": text}],
                "taskId": self.id,
                "contextId": self.context_id,
            }
            self.history.append(self.message)
        log(f"[a2a] task={self.id} {previous} -> {state}" + (f" ({text})" if text else ""))


TASKS: dict[str, Task] = {}
host = MCPHost(MCP_URL)


class RPCError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- parsing the fixed-format request -------------------------------------------

_REQUEST = re.compile(r"^\s*reservar\s+(?P<fields>.+?)\s*$", re.IGNORECASE)
_FIELD = re.compile(r"(\w+)=(\S+)")
_CHOICE = re.compile(r"^\s*escolha=(?P<value>\S+)\s*$")


def parse_request(text: str) -> dict[str, str] | None:
    match = _REQUEST.match(text)
    if not match:
        return None
    fields = dict(_FIELD.findall(match.group("fields")))
    return fields if {"sala", "inicio", "fim", "responsavel"} <= fields.keys() else None


def parse_choice(text: str) -> str | None:
    match = _CHOICE.match(text)
    return match.group("value") if match else None


def message_text(message: dict[str, Any]) -> str:
    return " ".join(p.get("text", "") for p in message.get("parts") or [] if isinstance(p, dict)).strip()


def trace_from_header(value: str | None) -> tuple[str, str] | None:
    """Reads the trace-id and flags from a valid W3C traceparent."""
    match = re.fullmatch(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})", (value or "").strip())
    if not match or match.group(1) == "0" * 32:
        return None
    return match.group(1), match.group(3)


# --- the bridge ----------------------------------------------------------------


def alternatives_line(question: Question) -> str:
    return "alternativas: " + ", ".join(question.options)


async def apply_result(task: Task, result: types.CallToolResult | types.InputRequiredResult, tool: str,
                       arguments: dict[str, Any]) -> None:
    """Translates the result of an MCP tools/call into the A2A Task state."""
    if isinstance(result, types.InputRequiredResult):
        # BRIDGE (outbound): MCP input_required becomes TASK_STATE_INPUT_REQUIRED. The
        # requestState is kept in the Task, opaque, and only goes back to the MCP server.
        question = question_from(result)
        task.pause = Pause(tool=tool, arguments=arguments, question=question)
        task.transition(INPUT_REQUIRED, alternatives_line(question))
        return

    task.pause = None
    if result.is_error:
        task.transition(FAILED, text_of(result) or "the tool returned an error")
        return
    data = result.structured_content or {}
    if data.get("reservado") is False:
        task.transition(CANCELED, f"Booking not made: {data.get('motivo') or 'declined'}.")
        return
    booking = {
        "reserva": data.get("reserva"),
        "sala": data.get("sala"),
        "inicio": data.get("inicio"),
        "fim": data.get("fim"),
        "responsavel": data.get("responsavel"),
        "politica": task.policy,
    }
    task.artifacts.append(
        {"artifactId": f"art-{secrets.token_hex(6)}", "name": "reserva", "parts": [{"text": json.dumps(booking)}]}
    )
    task.transition(COMPLETED, f"Booking {booking['reserva']} confirmed in {booking['sala']}.")


async def start(task: Task, text: str) -> None:
    request = parse_request(text)
    if request is None:
        task.transition(REJECTED, f"Request does not match the expected format. Use: {REQUEST_FORMAT}")
        return
    task.transition(WORKING)
    trace = (task.trace_id, task.trace_flags)
    tools = await host.list_tools(trace)
    tool = tools.get(BOOKING_TOOL)
    if tool is None:
        task.transition(FAILED, f"The MCP server does not offer the {BOOKING_TOOL} tool")
        return
    task.policy = await host.policy_version(trace)
    # Arguments come from the discovered inputSchema, not from a hardcoded list.
    expected = list((tool.input_schema or {}).get("properties", {}))
    missing = [name for name in (tool.input_schema or {}).get("required", []) if name not in request]
    if missing:
        task.transition(REJECTED, f"Request is missing {', '.join(missing)}. Use: {REQUEST_FORMAT}")
        return
    arguments = {name: request[name] for name in expected if name in request}
    result = await host.call(BOOKING_TOOL, arguments, trace)
    await apply_result(task, result, BOOKING_TOOL, arguments)


async def resume(task: Task, text: str) -> None:
    pause = task.pause
    assert pause is not None
    choice = parse_choice(text)
    if choice == "recusar":
        answer = types.ElicitResult(action="decline")
    elif choice in pause.question.options:
        answer = types.ElicitResult(action="accept", content={pause.question.field: choice})
    else:
        # Choice outside the enum: the Task stays paused and repeats the question.
        task.transition(INPUT_REQUIRED, alternatives_line(pause.question))
        return
    task.transition(WORKING)
    # BRIDGE (return): the same tools/call, with a new JSON-RPC id, carrying the answer
    # under the same inputRequests key and the requestState echoed unmodified.
    result = await host.call(
        pause.tool,
        pause.arguments,
        (task.trace_id, task.trace_flags),
        input_responses={pause.question.key: answer},
        request_state=pause.question.request_state,
    )
    await apply_result(task, result, pause.tool, pause.arguments)


async def run_guarded(task: Task, step) -> None:
    try:
        await step
    except MCPError as error:
        task.pause = None
        task.transition(FAILED, f"MCP server error ({error.error.code}): {error.error.message}")
    except Exception as error:  # the A2A client always gets a consistent Task
        log(f"[a2a] task={task.id} unexpected error: {error!r}")
        task.pause = None
        if task.state not in TERMINAL:
            task.transition(FAILED, f"Agent internal error: {error}")


# --- JSON-RPC --------------------------------------------------------------------


async def send_message(params: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
    message = params.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("parts"), list):
        raise RPCError(-32602, "params.message with parts is required")
    text = message_text(message)
    trace = trace_from_header(traceparent)
    task_id = message.get("taskId")

    if task_id:
        task = TASKS.get(task_id)
        if task is None:
            raise RPCError(TASK_NOT_FOUND, f"Task not found: {task_id}")
        async with task.lock:
            if task.state in TERMINAL:
                raise RPCError(UNSUPPORTED_OPERATION, f"Task {task_id} already ended in {task.state}")
            if task.state != INPUT_REQUIRED or task.pause is None:
                raise RPCError(UNSUPPORTED_OPERATION, f"Task {task_id} is not waiting for input")
            if trace:
                task.trace_id, task.trace_flags = trace
            task.history.append(message)
            await run_guarded(task, resume(task, text))
            return {"task": task.to_json()}

    task = Task(
        id=f"task-{secrets.token_hex(6)}",
        context_id=message.get("contextId") or f"ctx-{secrets.token_hex(6)}",
    )
    task.trace_id, task.trace_flags = trace or (secrets.token_hex(16), "01")
    task.history.append(message)
    TASKS[task.id] = task
    log(f"[a2a] task={task.id} created in {SUBMITTED} trace-id={task.trace_id}")
    async with task.lock:
        await run_guarded(task, start(task, text))
    return {"task": task.to_json()}


async def get_task(params: dict[str, Any], _traceparent: str | None) -> dict[str, Any]:
    task = TASKS.get(str(params.get("id", "")))
    if task is None:
        raise RPCError(TASK_NOT_FOUND, f"Task not found: {params.get('id')}")
    return {"task": task.to_json()}


METHODS = {"SendMessage": send_message, "GetTask": get_task}


def rpc_response(id_: Any, *, result: Any = None, error: tuple[int, str] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        body["error"] = {"code": error[0], "message": error[1]}
    else:
        body["result"] = result
    return JSONResponse(body)


async def a2a_endpoint(request: Request) -> JSONResponse:
    try:
        body = json.loads(await request.body())
    except ValueError:
        return rpc_response(None, error=(-32700, "Invalid JSON"))
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
        return rpc_response(body.get("id") if isinstance(body, dict) else None, error=(-32600, "Invalid request"))
    id_, method = body.get("id"), body["method"]
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    traceparent = request.headers.get("traceparent")
    log(f"[a2a] method={method} id={id_} traceparent={traceparent or '-'}")
    handler = METHODS.get(method)
    if handler is None:
        return rpc_response(id_, error=(-32601, f"Unsupported method: {method}"))
    try:
        return rpc_response(id_, result=await handler(params, traceparent))
    except RPCError as error:
        return rpc_response(id_, error=(error.code, error.message))


async def agent_card(_request: Request) -> JSONResponse:
    return JSONResponse(AGENT_CARD)


@asynccontextmanager
async def lifespan(_app: Starlette):
    await host.open()
    log(f"[a2a] central-de-salas at {PUBLIC_URL}/a2a, MCP host of {MCP_URL}")
    try:
        yield
    finally:
        await host.close()


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/a2a", a2a_endpoint, methods=["POST"]),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
