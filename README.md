# A Ponte: an A2A agent with MCP inside

Submission for the "A Ponte" ("The Bridge") challenge (Hill Valley Tech, five meeting rooms). It consists of two separate Python processes:

| Process | Folder | Port | Endpoint | Role |
|---|---|---|---|---|
| MCP server `central-de-salas` | [servidor-mcp/](servidor-mcp/) | 7301 | `POST /mcp` (Streamable HTTP, revision 2026-07-28, stateless) | 3 tools, 1 resource, MRTR on booking |
| Agent `Central de Salas` | [agente/](agente/) | 7300 | `POST /a2a` (A2A v1.0, JSON-RPC 2.0) and `GET /.well-known/agent-card.json` | MCP host on the inside, A2A server on the outside |

Stack: Python 3.12 with [uv](https://docs.astral.sh/uv/), the official `mcp==2.3.0` SDK (v2, aligned with revision 2026-07-28), `starlette==1.8.0` and `uvicorn==0.54.0`. Versions are pinned in each `pyproject.toml` and in the committed `uv.lock` files. The agent uses no LLM and decides by rule.

The manual test walkthrough, step by step and with the expected output, is in [TESTES-MANUAIS.md](TESTES-MANUAIS.md).

## How to run (Como rodar)

Prerequisites: `git`, `python3` (3.10 or later, for the validator) and `uv` (install with `curl -LsSf https://astral.sh/uv/install.sh | sh`). `uv` downloads the projects' Python 3.12 if it is not installed.

```bash
git clone https://github.com/KaduMelo/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
```

**Terminal 1, MCP server.** It needs `REQUEST_STATE_SECRET` with at least 32 bytes of randomness. Generate your own and never commit the value:

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
cd servidor-mcp
uv run python servidor.py
```

Without the secret, or with fewer than 32 bytes, the process refuses to start and prints the command above. To test a retry after a restart, restart the server in the same terminal with the same `REQUEST_STATE_SECRET` exported: a `requestState` sealed with a different key is rejected.

**Terminal 2, agent:**

```bash
cd agente
uv run python agente.py
```

**Terminal 3, validator** (from the repository root, with both processes freshly started):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

The MCP server's stderr (terminal 1) logs one line per request, with the method, id, tool or resource name, the elicitation capability the client declared, and the traceparent. The retry of a paused booking is tagged `retry=yes` and has a different id from the initial request:

```
[mcp] method=tools/list id=1 elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-4c2b7e4dcdc2c617-01
[mcp] method=resources/read id=2 name=politica://uso elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-f362f91e9ad01d95-01
[mcp] method=tools/call id=3 name=reservar_sala elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-3f3a4c23856c99c9-01
...
[mcp] method=tools/call id=12 name=reservar_sala elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-ad8d6af9c2f5232a-01
[mcp] method=tools/call id=13 name=reservar_sala retry=yes elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-4eb10c9a0e92acb6-01
```

The agent's stderr (terminal 2) logs every A2A request and every Task state transition.

Optional environment variables, with the defaults required by the challenge:

| Variable | Default | Process |
|---|---|---|
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `7301` | MCP server |
| `AGENTE_HOST` / `AGENTE_PORT` | `127.0.0.1` / `7300` | agent |
| `AGENTE_URL` | `http://127.0.0.1:7300` | public URL published in the Agent Card |
| `MCP_URL` | `http://127.0.0.1:7301/mcp` | MCP server the agent consumes |

## Where the bridge happens (Onde a ponte acontece)

The whole bridge lives in [agente/agente.py](agente/agente.py), at two points marked with a `PONTE` comment.

**Outbound, from `input_required` to the pause.** In [agente/agente.py:184-192](agente/agente.py#L184-L192), `concluir_com` receives the `tools/call` result. When it is an `InputRequiredResult`, `pergunta_de` ([agente/host_mcp.py:125](agente/host_mcp.py#L125)) extracts the `inputRequests` key, the field name and the `enum` of the form-mode elicitation. The Task stores the tool, the arguments, that key and the `requestState` in `task.pausa` without opening it, and moves to `TASK_STATE_INPUT_REQUIRED` with the message `alternativas: <ids>`, in enum order. The `requestState` arrives raw because the MCP host calls `session.call_tool(..., allow_input_required=True)` ([agente/host_mcp.py:121](agente/host_mcp.py#L121)) and registers no elicitation callback, so the SDK never closes the loop on its own.

**Return, from `escolha=` to the retry.** In [agente/agente.py:241-263](agente/agente.py#L241-L263), `retomar` translates `escolha=<id>` into `action: accept` and `escolha=recusar` into `action: decline`. A choice outside the enum keeps the Task paused and repeats the list. The agent then reissues the same `tools/call`, carrying `inputResponses` under the same key it received and the `requestState` echoed byte for byte. The SDK mints a new JSON-RPC id for every call, so the initial request and the retry have different ids, as the log above shows. The result goes back to `concluir_com`, which moves the Task to `COMPLETED` (with the `reserva` artifact), `CANCELED` (declined) or `FAILED` (`isError` or a protocol error).

On the server side, the bridge's counterpart is the `escolha_de_sala` resolver ([servidor-mcp/servidor.py:200](servidor-mcp/servidor.py#L200)), bound to the `escolha` parameter of `reservar_sala` through `Annotated[..., Resolve(escolha_de_sala)]`. When the room is taken, it returns an `Elicit` with the schema `{sala: enum[alternatives]}`. From there the SDK does the rest: it answers `resultType: input_required`, seals the `requestState`, rejects with -32021 any client without `elicitation.form` and, on the retry, verifies the state and injects the answer. There is no back channel and no open call waiting for the user.

## Technical decisions (Decisões técnicas)

**`requestState` protection.** I use the SDK's own utility, `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600)` ([servidor-mcp/servidor.py:184](servidor-mcp/servidor.py#L184)), which `MCPServer` installs as boundary middleware. It works like this:

- The token is AES-256-GCM (AEAD) with a key derived from the secret via HKDF-SHA256, in the format `v1.<base64url>`. It is both encrypted and authenticated, so changing any character breaks the GCM tag.
- The sealed envelope carries `iat` and `exp`, the method, the tool name, a SHA-256 digest of the original arguments, and the audience `central-de-salas`.
- Verification rejects a token that was tampered with, has expired, was minted for another tool, comes with different arguments, or belongs to another audience, always with `-32602 Invalid or expired requestState` and HTTP 400. The real reason goes only to the log.
- Tampered arguments on the retry therefore take the "server rejects the state" path.
- The secret comes only from the environment variable. The process will not start without it, and the SDK requires at least 32 bytes.

**Lifetime.** 10 minutes (`VALIDADE_REQUEST_STATE = 10 * 60`), within the 5 to 30 minute range the challenge asks for. I tested it with a server instance whose verifier clock was moved 11 minutes ahead: the same token was rejected with -32602, and the log recorded `requestState rejected on tools/call: expired`. With the normal clock, the retry completed.

**Nothing kept between rounds.** The server holds no state between the `input_required` and the retry. On the retry, the resolver runs again from the arguments, and the SDK accepts the answer only if the rendered question is identical to the one sealed in the state (the question digest travels inside the `requestState`). Since the key comes from the environment, a retry after restarting the process works. Bookings live in memory and do not survive a restart, as the challenge allows.

**Task state.** It lives in memory in the agent process, in the `TASKS` dictionary in [agente/agente.py](agente/agente.py), with one `asyncio.Lock` per Task. The paused state (`Pausa`: tool, arguments, key, field, alternatives and `requestState`) is per Task, which is why two Tasks paused at the same time never swap state. `Task.para_json()` never serializes the `Pausa`, so the `requestState` never shows up in the card, the artifact, the history or any message. Terminal states (`COMPLETED`, `CANCELED`, `FAILED`, `REJECTED`) are final: a `SendMessage` to a terminal Task gets error `-32004` (UnsupportedOperation), and an unknown Task gets `-32001` (TaskNotFound).

**MCP host.** The agent keeps a single long-lived `mcp.Client`, pinned to version `2026-07-28`, so it never sends `initialize` or `server/discover`. Every request carries the version, the `clientInfo` and the capabilities in its own `_meta`, and the SDK mirrors `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` into the headers. For each Task, the agent:

1. calls `tools/list` and builds the arguments from the discovered `inputSchema`, with no hardcoded list;
2. reads `politica://uso` and extracts the `versao` from its first line, which goes into the artifact;
3. calls `reservar_sala`.

The trace-id from the A2A client's `traceparent` header is propagated in `_meta.traceparent` on every MCP request of that Task, with a new span-id on each one. Without the header, the agent generates one trace-id per Task.

**A2A without an A2A SDK.** The JSON-RPC binding (`SendMessage`, `GetTask`) and the Agent Card are hand-written on Starlette, following `exemplos/wire/07` to `10`, because the binding is small and this keeps the wire format exactly as the contract shows. A request outside the fixed format ends in `TASK_STATE_REJECTED`.

**SDK limitations, documented instead of worked around:**

- **Elicitation capability.** The SDK's `ClientSession` only advertises elicitation when it is given an `elicitation_callback`. In that case it advertises `form` and `url` together and starts answering the questions by itself (`mcp/client/session.py`, `_build_capabilities`):
  ```python
  elicitation = (
      types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
      if self._elicitation_callback is not _default_elicitation_callback
      else None
  )
  ```
  To declare exactly `{"elicitation": {"form": {}}}` without a callback, the agent uses a session subclass that overrides only `_build_capabilities` ([agente/host_mcp.py:29](agente/host_mcp.py#L29)). The rest of the protocol is still the SDK's.
- **Unknown tool.** The SDK's `MCPServer` rejects the call with `isError: true` (`Unknown tool: ...`) instead of -32602, a form the challenge accepts.
- **`inputRequests` key.** The SDK derives the key from the resolver's `module:qualname`. Since the server runs as a script, it becomes `__main__:escolha_de_sala`, the same as in `exemplos/wire/03`.

## Validator output (Saída do validador)

Last run, from a clean clone of the fork (`git clone` followed only by the commands in [How to run](#how-to-run-como-rodar)), with both processes freshly started:

```
trace-id desta execucao: 4f0ca2752827b37e2e79d3b83d2af0cf
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes

(exit code: 0)
```
