# Manual tests, milestone by milestone

A walkthrough to reproduce each milestone of the submission with `curl`. JSON processing uses `python3`; `jq` is not needed. The helper functions live in [test-helpers.sh](test-helpers.sh) and are thin shortcuts over `curl`:

| Function | What it does |
|---|---|
| `body FILE [path=value ...]` | prints the `request.body` of `exemplos/wire/FILE`, with fields replaced (`path=-` removes one) |
| `mcp_post BODY` | `POST /mcp` with `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` mirrored from the body, then prints `HTTP nnn` |
| `a2a_post BODY` / `a2a_send TEXT [TASK]` / `a2a_get TASK` | A2A JSON-RPC calls to the agent, with `traceparent: $TP` |
| `get_field PATH` | extracts one field from the JSON (`result.task.id`) |
| `task_summary` | shows the id, state, status message and artifact of an A2A response |

The trace-id used in the examples is the one from `exemplos/wire`: `4bf92f3577b34da6a3ce929d0e0e4736`. Task, context and message ids and the requestStates change on every run; everything else should come out the same.

A few strings stay in Portuguese because the challenge fixes them byte for byte and the validator checks them: the folder names (`servidor-mcp/`, `agente/`, `dados/`), tool, argument, field and skill names (`reservar_sala`, `sala`, `reservar-sala`), the request formats (`reservar sala=...`, `escolha=...`), the `alternativas: ...` line, the policy text in `dados/` and the tool error messages (`Sala inexistente: ...`).

## 0. Setup

Three terminals, all at the repository root.

```bash
# terminal 1: MCP server (keep its stderr visible)
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
cd servidor-mcp && uv run python server.py
```

```bash
# terminal 2: agent
cd agente && uv run python agent.py
```

```bash
# terminal 3: tests
source ./test-helpers.sh
```

Bookings live in memory. Each section below says when to restart the processes (Ctrl+C and the same command again, in the same terminal) so the outputs match the ones shown here. Restart the MCP server **in terminal 1**, where `REQUEST_STATE_SECRET` is already exported.

---

## 1. Basic MCP server

With the MCP server freshly started.

**1.1 `tools/list` returns the three tools**

```bash
mcp_post "$(body 01-tools-list.json)" | python3 -c 'import json,sys; t=sys.stdin.read().split("\nHTTP "); print([x["name"] for x in json.loads(t[0])["result"]["tools"]], "HTTP", t[1].strip())'
```
```
['listar_salas', 'consultar_disponibilidade', 'reservar_sala'] HTTP 200
```
Run `mcp_post "$(body 01-tools-list.json)"` without the filter to see the `inputSchema` of each tool (all `type: object`) and the `outputSchema`, identical to those in `exemplos/wire/01`.

**1.2 `listar_salas`: structuredContent plus the same JSON as text**

```bash
mcp_post "$(body 02-tools-call-livre.json params.name=listar_salas params.arguments={})" | python3 -c 'import json,sys; r=json.loads(sys.stdin.read().split("\nHTTP ")[0])["result"]; print("rooms:", len(r["structuredContent"]["salas"]), "| text == structuredContent:", json.loads(r["content"][0]["text"]) == r["structuredContent"])'
```
```
rooms: 5 | text == structuredContent: True
```

**1.3 The `politica://uso` resource**

```bash
mcp_post "$(body 05-resources-read-politica.json)" | get_field result.contents.0
```
```
{"mimeType": "text/markdown", "text": "versao: 2026-11-01\n\n- Reservas somente entre 08:00 e 20:00, horario de Sao Paulo (-03:00).\n- Duracao maxima de 2 horas por reserva.\n- Uma sala nao pode ter duas reservas sobrepostas.\n", "uri": "politica://uso"}
```

**1.4 and 1.5 Incomplete `_meta`: -32602 with HTTP 400**

```bash
mcp_post "$(body 01-tools-list.json params._meta.io.modelcontextprotocol/protocolVersion=-)"
mcp_post "$(body 01-tools-list.json params._meta.io.modelcontextprotocol/clientCapabilities=-)"
```
```
{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"params._meta is missing the required envelope key(s): io.modelcontextprotocol/protocolVersion"}}
HTTP 400
{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"params._meta is missing the required envelope key(s): io.modelcontextprotocol/clientCapabilities"}}
HTTP 400
```

**1.6 Unknown resource: -32602**

```bash
mcp_post "$(body 05-resources-read-politica.json params.uri=politica://inexistente)"
```
```
{"jsonrpc":"2.0","id":5,"error":{"code":-32602,"message":"Unknown resource: politica://inexistente","data":{"uri":"politica://inexistente"}}}
HTTP 400
```

**1.7 An unknown tool is rejected** (the SDK uses `isError`, a form the challenge accepts)

```bash
mcp_post "$(body 02-tools-call-livre.json params.name=voar_delorean params.arguments={})"
```
```
{"jsonrpc":"2.0","id":2,"result":{"content":[{"text":"Unknown tool: voar_delorean","type":"text"}],"isError":true,"resultType":"complete","_meta":{"io.modelcontextprotocol/serverInfo":{"name":"central-de-salas","version":"1.0.0"}}}}
HTTP 200
```

**1.8 A header that does not match the body: -32020**

```bash
curl -s $MCP/mcp -w '\nHTTP %{http_code}\n' -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/list' -d "$(body 02-tools-call-livre.json)"
```
```
{"jsonrpc":"2.0","id":2,"error":{"code":-32020,"message":"mcp-method header does not match the request body's method"}}
HTTP 400
```

**In terminal 1**, every request shows up as one line with the method, id, name, elicitation capability and traceparent:

```
[mcp] method=tools/list id=1 elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
```

---

## 2. Usage policy and tool execution errors

Same server. `c ROOM START END` calls `consultar_disponibilidade` on 2026-11-03:

```bash
c() { mcp_post "$(body 02-tools-call-livre.json params.name=consultar_disponibilidade "params.arguments={\"sala\":\"$1\",\"inicio\":\"2026-11-03T$2:00-03:00\",\"fim\":\"2026-11-03T$3:00-03:00\"}")"; }

c sala-aquario 09:00 10:00 | get_field result.structuredContent   # 2.1 free
c sala-garagem 14:00 15:00 | get_field result.structuredContent   # 2.2 taken
c sala-delorean 09:00 10:00 | get_field result.content.0.text     # 2.3 unknown room
c sala-aquario 07:00 08:00 | get_field result.content.0.text      # 2.4 outside the allowed window
c sala-aquario 09:00 12:00 | get_field result.content.0.text      # 2.5 longer than 2 hours
c sala-aquario 10:00 09:00 | get_field result.content.0.text      # 2.6 reversed interval
```
```
{"sala": "sala-aquario", "livre": true, "conflitos": []}
{"sala": "sala-garagem", "livre": false, "conflitos": [{"id": "res-0001", "inicio": "2026-11-03T14:00:00-03:00", "fim": "2026-11-03T15:00:00-03:00", "responsavel": "Marty"}]}
Error executing tool consultar_disponibilidade: Sala inexistente: sala-delorean
Error executing tool consultar_disponibilidade: Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00
Error executing tool consultar_disponibilidade: Duracao acima do limite: a politica permite no maximo 2 horas
Error executing tool consultar_disponibilidade: Intervalo invalido: fim deve ser posterior a inicio
```
Errors 2.3 to 2.6 come with `isError: true` (run them without `get_field` to see it). `reservar_sala` applies the same validations.

---

## 3. MRTR on booking

Same server, still not restarted.

**3.1 Free slot: `complete` right away** (`exemplos/wire/02`)

```bash
mcp_post "$(body 02-tools-call-livre.json)" | get_field result.structuredContent
```
```
{"reserva": "res-0003", "reservado": true, "sala": "sala-aquario", "inicio": "2026-11-03T09:00:00-03:00", "fim": "2026-11-03T10:00:00-03:00", "responsavel": "Doc", "politica": "2026-11-01", "motivo": null}
```

**3.2 Conflict: `input_required` with one form-mode elicitation and a `requestState`** (`exemplos/wire/03`)

```bash
mcp_post "$(body 03-tools-call-conflito-input-required.json)" > /tmp/conflict.txt
get_field result.resultType < /tmp/conflict.txt
get_field result.inputRequests < /tmp/conflict.txt
STATE=$(get_field result.requestState < /tmp/conflict.txt); echo "${STATE:0:24}..."
KEY='__main__:room_choice'
ACCEPT="params.inputResponses={\"$KEY\":{\"action\":\"accept\",\"content\":{\"sala\":\"sala-fusca\"}}}"
```
```
input_required
{"__main__:room_choice": {"method": "elicitation/create", "params": {"message": "The requested room is taken in this interval. Choose an alternative.", "mode": "form", "requestedSchema": {"properties": {"sala": {"description": "Chosen alternative room", "enum": ["sala-fusca", "sala-mirante"], "title": "Room", "type": "string"}}, "required": ["sala"], "type": "object"}}}}
v1.U8D7eN_S7WOlgItV7u-7e...
```
The enum lists the free rooms with capacity ≥ 12, ordered by capacity and then id, at most three.

**3.3 The same conflict without declaring elicitation: -32021 with HTTP 400** (`exemplos/wire/06`)

```bash
mcp_post "$(body 06-erro-32021-sem-elicitation.json)"
```
```
{"jsonrpc":"2.0","id":6,"error":{"code":-32021,"message":"Client did not declare the form elicitation capability required by resolver '__main__:room_choice'","data":{"requiredCapabilities":{"elicitation":{"form":{}}}}}}
HTTP 400
```

**3.4 `requestState` with one character changed: -32602**

```bash
TAMPERED="${STATE:0:40}$( [ "${STATE:40:1}" = A ] && echo B || echo A )${STATE:41}"
mcp_post "$(body 04-tools-call-retry.json params.requestState=$TAMPERED "$ACCEPT")"
```
```
{"jsonrpc":"2.0","id":4,"error":{"code":-32602,"message":"Invalid or expired requestState","data":{"reason":"invalid_request_state"}}}
HTTP 400
```
Terminal 1 shows the real reason: `requestState rejected on tools/call: seal`.

**3.5 Retry with tampered arguments (`responsavel=Biff`): rejected, nothing booked**

```bash
mcp_post "$(body 04-tools-call-retry.json params.requestState=$STATE "$ACCEPT" params.arguments.responsavel=Biff)"
```
```
{"jsonrpc":"2.0","id":4,"error":{"code":-32602,"message":"Invalid or expired requestState","data":{"reason":"invalid_request_state"}}}
HTTP 400
```
In terminal 1: `requestState rejected on tools/call: request binding`.

**3.6 Valid retry after restarting the MCP server** (`exemplos/wire/04`)

Restart the server in terminal 1 (Ctrl+C, then `uv run python server.py`, with the same secret exported). Then:

```bash
mcp_post "$(body 04-tools-call-retry.json params.requestState=$STATE "$ACCEPT")"
```
```
{"jsonrpc":"2.0","id":4,"result":{"content":[{"text":"{\n  \"reserva\": \"res-0003\",\n  \"reservado\": true,\n  \"sala\": \"sala-fusca\", ...}"}],"isError":false,"resultType":"complete","structuredContent":{"reserva":"res-0003","reservado":true,"sala":"sala-fusca","inicio":"2026-11-03T14:00:00-03:00","fim":"2026-11-03T15:00:00-03:00","responsavel":"Marty","politica":"2026-11-01","motivo":null},...}
HTTP 200
```
The new process had nothing in memory: everything came from the `requestState` and from the arguments it seals. The retry id (4) differs from the request id (3).

**3.7 Decline (`action: decline`): `complete`, no `isError`, `reservado: false`** (`exemplos/wire/11`)

```bash
R=$(mcp_post "$(body 11-tools-call-retry-recusa.json params.inputResponses=- params.requestState=-)"); get_field result.resultType <<< "$R"
mcp_post "$(body 11-tools-call-retry-recusa.json params.requestState=$(get_field result.requestState <<< "$R"))" | get_field result.structuredContent
```
```
input_required
{"reserva": null, "reservado": false, "sala": null, "inicio": null, "fim": null, "responsavel": null, "politica": null, "motivo": "declined"}
```

**3.8 Conflict with no possible alternative: `isError`, no elicitation**

```bash
r() { mcp_post "$(body 02-tools-call-livre.json 'params.arguments={"sala":"sala-mirante","inicio":"2026-11-03T11:00:00-03:00","fim":"2026-11-03T12:00:00-03:00","responsavel":"Doc"}')"; }
r | get_field result.structuredContent.reserva   # the first booking goes through
r | get_field result                             # the second conflicts, and no room has capacity >= 20
```
```
res-0004
{"content": [{"text": "Error executing tool reservar_sala: Sem alternativas disponiveis no intervalo", "type": "text"}], "isError": true, "resultType": "complete", ...}
```

**3.9 Expiry.** The `requestState` is valid for 10 minutes (`REQUEST_STATE_TTL` in [servidor-mcp/server.py](servidor-mcp/server.py)). To check it, create a conflict as in 3.2, wait more than 10 minutes and resend the retry: the response is the same as in 3.4, and terminal 1 shows `requestState rejected on tools/call: expired`.

---

## 4. The agent as an MCP host

**Restart both processes** (terminal 1 and terminal 2). Then:

```bash
P() { echo "reservar sala=$1 inicio=2026-11-03T$2:00-03:00 fim=2026-11-03T$3:00-03:00 responsavel=$4"; }
a2a_send "$(P sala-porao 09:00 10:00 Doc)" | task_summary
```
```
task     : task-d495471baa28 | context: ctx-827aad500bec
state    : TASK_STATE_COMPLETED
message  : Booking res-0003 confirmed in sala-porao.
artifact : reserva {"reserva": "res-0003", "sala": "sala-porao", "inicio": "2026-11-03T09:00:00-03:00", "fim": "2026-11-03T10:00:00-03:00", "responsavel": "Doc", "politica": "2026-11-01"}
```

In **terminal 1** (MCP server), check:

```
[mcp] method=tools/list id=1 elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-4c2b7e4dcdc2c617-01
[mcp] method=resources/read id=2 name=politica://uso elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-f362f91e9ad01d95-01
[mcp] method=tools/call id=3 name=reservar_sala elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-3f3a4c23856c99c9-01
```

- `tools/list` comes before the first `tools/call`: the agent discovers the tools at runtime and builds the arguments from the `inputSchema`.
- `resources/read` of `politica://uso`: the version `2026-11-01` comes from here and goes into the artifact's `politica` field.
- `elicitation=form`: every agent request declares `{"elicitation": {"form": {}}}` in `_meta`.
- The traceparent carries the same trace-id (`4bf92f35...`) as the header sent to the agent, with a new span-id on every request.

**Terminal 2** (agent) shows the transitions `SUBMITTED -> WORKING -> COMPLETED`.

---

## 5. The agent as an A2A server

Same processes as section 4.

```bash
# 5.1 Agent Card
curl -s $AGENT/.well-known/agent-card.json | python3 -c 'import json,sys; c=json.load(sys.stdin); print(c["name"], "|", json.dumps(c["supportedInterfaces"]), "|", [s["id"] for s in c["skills"]])'

# 5.2 free room: COMPLETED with an artifact
R=$(a2a_send "$(P sala-aquario 10:00 11:00 Doc)"); task_summary <<< "$R"; T=$(get_field result.task.id <<< "$R")

# 5.3 GetTask: id, contextId, current state and artifact
a2a_get $T | python3 -c 'import json,sys; t=json.load(sys.stdin)["result"]["task"]; print(t["id"], t["contextId"], t["status"]["state"], [m["role"] for m in t["history"]], [a["name"] for a in t["artifacts"]])'

# 5.4 SendMessage to a terminal Task: error
a2a_send "escolha=sala-fusca" $T | task_summary

# 5.5 unknown room: FAILED with the tool's exact message
a2a_send "$(P sala-inexistente 09:00 10:00 Doc)" | task_summary

# 5.6 request outside the fixed format: REJECTED
a2a_send "quero uma sala amanha" | task_summary

# 5.7 GetTask for an unknown Task
a2a_get task-nao-existe | task_summary
```
```
Central de Salas | [{"url": "http://127.0.0.1:7300/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}] | ['reservar-sala']
task     : task-10f02f0c13a9 | context: ctx-bf1b7cf46304
state    : TASK_STATE_COMPLETED
message  : Booking res-0004 confirmed in sala-aquario.
artifact : reserva {"reserva": "res-0004", "sala": "sala-aquario", "inicio": "2026-11-03T10:00:00-03:00", "fim": "2026-11-03T11:00:00-03:00", "responsavel": "Doc", "politica": "2026-11-01"}
task-10f02f0c13a9 ctx-bf1b7cf46304 TASK_STATE_COMPLETED ['ROLE_USER', 'ROLE_AGENT'] ['reserva']
ERROR {"code": -32004, "message": "Task task-10f02f0c13a9 already ended in TASK_STATE_COMPLETED"}
task     : task-5fe9aeab9c5a | context: ctx-2ec71d6b446a
state    : TASK_STATE_FAILED
message  : Error executing tool reservar_sala: Sala inexistente: sala-inexistente
task     : task-911be093d9dc | context: ctx-66a3ed126025
state    : TASK_STATE_REJECTED
message  : Request does not match the expected format. Use: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>
ERROR {"code": -32001, "message": "Task not found: task-nao-existe"}
```
To compare the whole card against `exemplos/wire/07-a2a-agent-card.json`: `curl -s $AGENT/.well-known/agent-card.json | python3 -m json.tool`.

---

## 6. The bridge

Same processes.

```bash
# 6.1 taken room (exemplos/wire/08): the Task pauses with the alternatives line
R=$(a2a_post "$(body 08-a2a-send-message.json)"); task_summary <<< "$R"; T=$(get_field result.task.id <<< "$R")

# 6.2 GetTask reflects the pause
a2a_get $T | task_summary

# 6.3 choice outside the enum: stays paused and repeats the list
a2a_send "escolha=sala-aquario" $T | task_summary

# 6.4 valid choice (exemplos/wire/10): MCP retry with a new id, COMPLETED in the chosen room
a2a_send "escolha=sala-mirante" $T | task_summary
```
```
task     : task-7e9ea07d0c92 | context: ctx-40e1634c9057
state    : TASK_STATE_INPUT_REQUIRED
message  : alternativas: sala-fusca, sala-mirante
task     : task-7e9ea07d0c92 | context: ctx-40e1634c9057
state    : TASK_STATE_INPUT_REQUIRED
message  : alternativas: sala-fusca, sala-mirante
task     : task-7e9ea07d0c92 | context: ctx-40e1634c9057
state    : TASK_STATE_INPUT_REQUIRED
message  : alternativas: sala-fusca, sala-mirante
task     : task-7e9ea07d0c92 | context: ctx-40e1634c9057
state    : TASK_STATE_COMPLETED
message  : Booking res-0005 confirmed in sala-mirante.
artifact : reserva {"reserva": "res-0005", "sala": "sala-mirante", "inicio": "2026-11-03T14:00:00-03:00", "fim": "2026-11-03T15:00:00-03:00", "responsavel": "Marty", "politica": "2026-11-01"}
```

In **terminal 1**, the pair behind the pause: the initial `tools/call` and the retry, with different ids and the same trace-id. The invalid choice in 6.3 produced no MCP request: the agent checked it against the enum it already had.

```
[mcp] method=tools/call id=12 name=reservar_sala elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-ad8d6af9c2f5232a-01
[mcp] method=tools/call id=13 name=reservar_sala retry=yes elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-4eb10c9a0e92acb6-01
```

```bash
# 6.5 the same request again, then decline: CANCELED (sala-mirante was already booked in 6.4)
R=$(a2a_post "$(body 08-a2a-send-message.json)"); task_summary <<< "$R"; T=$(get_field result.task.id <<< "$R")
a2a_send "escolha=recusar" $T | task_summary
```
```
task     : task-ca43fdfce8a3 | context: ctx-fb415795e683
state    : TASK_STATE_INPUT_REQUIRED
message  : alternativas: sala-fusca
task     : task-ca43fdfce8a3 | context: ctx-fb415795e683
state    : TASK_STATE_CANCELED
message  : Booking not made: declined.
```

```bash
# 6.6 two Tasks paused at the same time, resumed in reverse order
A=$(a2a_send "$(P sala-fusca 16:00 17:00 Lorraine)" | get_field result.task)
B=$(a2a_send "$(P sala-garagem 14:00 15:00 George)" | get_field result.task)
for X in "$A" "$B"; do python3 -c 'import json,sys; t=json.loads(sys.argv[1]); print(t["id"], t["status"]["state"], t["status"]["message"]["parts"][0]["text"])' "$X"; done
TA=$(get_field id <<< "$A"); TB=$(get_field id <<< "$B")
a2a_send "escolha=sala-fusca" $TB | task_summary
a2a_send "escolha=sala-garagem" $TA | task_summary
```
```
task-d607260a659a TASK_STATE_INPUT_REQUIRED alternativas: sala-garagem, sala-mirante
task-04cadac2cdc8 TASK_STATE_INPUT_REQUIRED alternativas: sala-fusca
task     : task-04cadac2cdc8 | context: ctx-07a5e0ce6567
state    : TASK_STATE_COMPLETED
message  : Booking res-0006 confirmed in sala-fusca.
artifact : reserva {"reserva": "res-0006", "sala": "sala-fusca", "inicio": "2026-11-03T14:00:00-03:00", "fim": "2026-11-03T15:00:00-03:00", "responsavel": "George", "politica": "2026-11-01"}
task     : task-d607260a659a | context: ctx-ed8f5275f562
state    : TASK_STATE_COMPLETED
message  : Booking res-0007 confirmed in sala-garagem.
artifact : reserva {"reserva": "res-0007", "sala": "sala-garagem", "inicio": "2026-11-03T16:00:00-03:00", "fim": "2026-11-03T17:00:00-03:00", "responsavel": "Lorraine", "politica": "2026-11-01"}
```
Each Task completed with its own interval and its own requester: the `requestState`s did not get mixed up.

```bash
# 6.7 the requestState (prefix "v1.") appears in no A2A response
for t in $TA $TB $T; do a2a_get $t; done | grep -c '"v1\.'
```
```
0
```

---

## 7. Validator

**Restart both processes** and run, from the repository root:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301; echo "exit=$?"
```

Expected: `resumo: 36 passaram, 0 falharam, de 36 verificacoes` and `exit=0`. The full output of the last run is in the [README](README.md#validator-output). Then look in terminal 1 for the trace-id the validator printed on its first line: it appears in the `traceparent` of the `tools/list`, `resources/read` and `tools/call` requests issued by the agent.
