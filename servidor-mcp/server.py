"""Central de Salas MCP server (Streamable HTTP, revision 2026-07-28).

Exposes three tools (listar_salas, consultar_disponibilidade, reservar_sala) and
the politica://uso resource. When a booking conflicts, reservar_sala answers
input_required through the SDK's MRTR: the `room_choice` resolver returns an
`Elicit`, the SDK packs the question into `inputRequests` and seals the
`requestState` with AES-256-GCM (key from REQUEST_STATE_SECRET). Nothing is kept
in memory between the input_required and the retry.

Tool names, argument names, output fields and error messages are fixed by the
challenge contract (in Portuguese) and checked by the validator.
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

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "dados"))
SAO_PAULO = timezone(timedelta(hours=-3))
WINDOW_START, WINDOW_END = 8 * 60, 20 * 60
MAX_DURATION = timedelta(hours=2)
MAX_ALTERNATIVES = 3
REQUEST_STATE_TTL = 10 * 60  # seconds

# Exact messages required by the challenge.
ERR_WINDOW = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERR_DURATION = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERR_INTERVAL = "Intervalo invalido: fim deve ser posterior a inicio"
ERR_NO_ALTERNATIVE = "Sem alternativas disponiveis no intervalo"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --- domain: starter data, bookings in memory -------------------------------

ROOMS: list[dict[str, Any]] = json.loads((DATA_DIR / "salas.json").read_text(encoding="utf-8"))
ROOMS_BY_ID = {r["id"]: r for r in ROOMS}
BOOKINGS: list[dict[str, Any]] = json.loads((DATA_DIR / "reservas.json").read_text(encoding="utf-8"))
POLICY_TEXT = (DATA_DIR / "politica-de-uso.md").read_text(encoding="utf-8")
POLICY_VERSION = POLICY_TEXT.splitlines()[0].split(":", 1)[1].strip()


def _instant(value: str) -> datetime:
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        raise ToolError(f"Invalid date: {value}") from None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=SAO_PAULO)
    return instant.astimezone(SAO_PAULO)


def _minutes(instant: datetime) -> int:
    return instant.hour * 60 + instant.minute + (1 if instant.second or instant.microsecond else 0)


def _in_window(instant: datetime) -> bool:
    return WINDOW_START <= _minutes(instant) <= WINDOW_END


def validate_request(room: str, start: str, end: str) -> tuple[datetime, datetime]:
    """Usage policy rules, in order: room, interval, window, duration."""
    if room not in ROOMS_BY_ID:
        raise ToolError(f"Sala inexistente: {room}")
    begin, finish = _instant(start), _instant(end)
    if finish <= begin:
        raise ToolError(ERR_INTERVAL)
    if begin.date() != finish.date() or not _in_window(begin) or not _in_window(finish):
        raise ToolError(ERR_WINDOW)
    if finish - begin > MAX_DURATION:
        raise ToolError(ERR_DURATION)
    return begin, finish


def conflicts(room: str, begin: datetime, finish: datetime) -> list[dict[str, Any]]:
    return [
        b
        for b in BOOKINGS
        if b["sala"] == room and _instant(b["inicio"]) < finish and begin < _instant(b["fim"])
    ]


def alternatives(room: str, begin: datetime, finish: datetime) -> list[str]:
    """Free rooms with capacity >= the requested one, by capacity then id, at most three."""
    minimum = ROOMS_BY_ID[room]["capacidade"]
    free = [
        r for r in ROOMS if r["id"] != room and r["capacidade"] >= minimum and not conflicts(r["id"], begin, finish)
    ]
    free.sort(key=lambda r: (r["capacidade"], r["id"]))
    return [r["id"] for r in free[:MAX_ALTERNATIVES]]


def create_booking(room: str, start: str, end: str, owner: str) -> dict[str, Any]:
    booking = {
        "id": f"res-{len(BOOKINGS) + 1:04d}",
        "sala": room,
        "inicio": start,
        "fim": end,
        "responsavel": owner,
    }
    BOOKINGS.append(booking)
    return booking


# --- output contracts (field names fixed by the challenge) -------------------


class RoomOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class RoomList(BaseModel):
    salas: list[RoomOut]


class ConflictOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Availability(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflictOut]


class BookingOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class RoomChoice(BaseModel):
    sala: str


def choice_schema(options: list[str]) -> type[RoomChoice]:
    """Flat elicitation schema: `sala` restricted to the alternatives, in computed order."""
    return create_model(
        "RoomChoice",
        __base__=RoomChoice,
        sala=(Literal[tuple(options)], Field(title="Room", description="Chosen alternative room")),
    )


# --- MCP server -------------------------------------------------------------


def _secret() -> str:
    secret = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(secret.encode()) < 32:
        sys.exit(
            "REQUEST_STATE_SECRET is missing or shorter than 32 bytes. Generate one with:\n"
            '  export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")'
        )
    return secret


mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[_secret()], ttl=REQUEST_STATE_TTL),
)


# The tool functions and their parameters keep the contract's names: they become
# the tool names and the inputSchema properties on the wire.


@mcp.tool(description="Lists all rooms with their capacity and equipment.")
def listar_salas() -> RoomList:
    return RoomList(salas=[RoomOut(**r) for r in ROOMS])


@mcp.tool(description="Tells whether a room is free in the interval, and which bookings conflict.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Availability:
    begin, finish = validate_request(sala, inicio, fim)
    found = [ConflictOut(**{k: b[k] for k in ("id", "inicio", "fim", "responsavel")}) for b in conflicts(sala, begin, finish)]
    return Availability(sala=sala, livre=not found, conflitos=found)


def room_choice(sala: str, inicio: str, fim: str) -> Elicit[RoomChoice] | None:
    """MRTR resolver: asks only when the requested room is taken.

    Runs again on every round (including after a restart); the client's answer is
    accepted only if the rendered question is identical to the one that was sealed.
    """
    begin, finish = validate_request(sala, inicio, fim)
    if not conflicts(sala, begin, finish):
        return None
    options = alternatives(sala, begin, finish)
    if not options:
        raise ToolError(ERR_NO_ALTERNATIVE)
    return Elicit("The requested room is taken in this interval. Choose an alternative.", choice_schema(options))


@mcp.tool(description="Books a room. If the interval is taken, asks which alternative to use.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    choice: Annotated[ElicitationResult[RoomChoice], Resolve(room_choice)],
) -> BookingOut:
    if not isinstance(choice, AcceptedElicitation):
        reason = "declined" if choice.action == "decline" else "canceled"
        return BookingOut(reservado=False, motivo=reason)
    target = choice.data.sala if choice.data is not None else sala
    begin, finish = validate_request(target, inicio, fim)
    if conflicts(target, begin, finish):
        raise ToolError(ERR_NO_ALTERNATIVE)
    booking = create_booking(target, inicio, fim, responsavel)
    return BookingOut(
        reserva=booking["id"],
        sala=target,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=POLICY_VERSION,
    )


@mcp.resource("politica://uso", name="usage-policy", mime_type="text/markdown",
              description="Meeting room usage policy.")
def usage_policy() -> str:
    return POLICY_TEXT


# --- one stderr log line per request ---------------------------------------


class RequestLogger:
    """ASGI: logs method, id, name and traceparent of every POST received, before the SDK."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        self._record(body)
        delivered = False

        async def replay() -> dict:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    def _record(body: bytes) -> None:
        try:
            msg = json.loads(body)
        except ValueError:
            log("[mcp] request with an invalid body")
            return
        if not isinstance(msg, dict):
            log("[mcp] request is not JSON-RPC")
            return
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        extra = ""
        if params.get("name") or params.get("uri"):
            extra += f" name={params.get('name') or params.get('uri')}"
        if "requestState" in params:
            extra += " retry=yes"
        capabilities = meta.get("io.modelcontextprotocol/clientCapabilities")
        if isinstance(capabilities, dict):
            elicitation = capabilities.get("elicitation")
            extra += f" elicitation={'+'.join(sorted(elicitation)) or 'empty' if isinstance(elicitation, dict) else 'none'}"
        log(
            f"[mcp] method={msg.get('method')} id={msg.get('id')}{extra} "
            f"traceparent={meta.get('traceparent', '-')}"
        )


app = RequestLogger(mcp.streamable_http_app(json_response=True, stateless_http=True))


def main() -> None:
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "7301"))
    log(f"[mcp] central-de-salas at http://{host}:{port}/mcp (policy {POLICY_VERSION})")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
