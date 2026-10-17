# Testes manuais, marco a marco

Roteiro para reproduzir cada marco da entrega com `curl`. O processamento de JSON usa `python3`; não precisa de `jq`. As funções de apoio ficam em [testes.sh](testes.sh) e são só atalhos finos sobre `curl`:

| Função | O que faz |
|---|---|
| `corpo ARQ [caminho=valor ...]` | imprime o `request.body` de `exemplos/wire/ARQ`, com campos trocados (`caminho=-` remove) |
| `mcp_post CORPO` | `POST /mcp` com `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` espelhados do corpo, e imprime `HTTP nnn` no fim |
| `a2a_post CORPO` / `a2a_enviar TEXTO [TASK]` / `a2a_get TASK` | JSON-RPC A2A no agente, com `traceparent: $TP` |
| `pegar CAMINHO` | extrai um campo do JSON (`result.task.id`) |
| `resumo_task` | mostra id, estado, mensagem e artifact de uma resposta A2A |

O trace-id usado nos exemplos é o de `exemplos/wire`: `4bf92f3577b34da6a3ce929d0e0e4736`. Os ids de Task, contexto e mensagem e os requestStates mudam a cada execução; o resto das saídas deve sair igual.

## 0. Preparação

Três terminais, todos na raiz do repositório.

```bash
# terminal 1: servidor MCP (deixe o stderr visivel)
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
cd servidor-mcp && uv run python servidor.py
```

```bash
# terminal 2: agente
cd agente && uv run python agente.py
```

```bash
# terminal 3: testes
source ./testes.sh
```

As reservas ficam em memória. Cada seção abaixo diz quando reiniciar os processos (Ctrl+C e o mesmo comando de novo, no mesmo terminal) para que as saídas batam com as mostradas aqui. Reinicie o servidor MCP **no terminal 1**, onde o `REQUEST_STATE_SECRET` já está exportado.

---

## 1. Servidor MCP básico

Com o servidor MCP recém-iniciado.

**1.1 `tools/list` traz as três tools**

```bash
mcp_post "$(corpo 01-tools-list.json)" | python3 -c 'import json,sys; t=sys.stdin.read().split("\nHTTP "); print([x["name"] for x in json.loads(t[0])["result"]["tools"]], "HTTP", t[1].strip())'
```
```
['listar_salas', 'consultar_disponibilidade', 'reservar_sala'] HTTP 200
```
Rode `mcp_post "$(corpo 01-tools-list.json)"` sem o filtro para ver os `inputSchema` (todos `type: object`) e os `outputSchema`, iguais aos de `exemplos/wire/01`.

**1.2 `listar_salas`: structuredContent e o mesmo JSON em texto**

```bash
mcp_post "$(corpo 02-tools-call-livre.json params.name=listar_salas params.arguments={})" | python3 -c 'import json,sys; r=json.loads(sys.stdin.read().split("\nHTTP ")[0])["result"]; print("salas:", len(r["structuredContent"]["salas"]), "| texto == structuredContent:", json.loads(r["content"][0]["text"]) == r["structuredContent"])'
```
```
salas: 5 | texto == structuredContent: True
```

**1.3 Resource `politica://uso`**

```bash
mcp_post "$(corpo 05-resources-read-politica.json)" | pegar result.contents.0
```
```
{"mimeType": "text/markdown", "text": "versao: 2026-11-01\n\n- Reservas somente entre 08:00 e 20:00, horario de Sao Paulo (-03:00).\n- Duracao maxima de 2 horas por reserva.\n- Uma sala nao pode ter duas reservas sobrepostas.\n", "uri": "politica://uso"}
```

**1.4 e 1.5 `_meta` incompleto: -32602 e HTTP 400**

```bash
mcp_post "$(corpo 01-tools-list.json params._meta.io.modelcontextprotocol/protocolVersion=-)"
mcp_post "$(corpo 01-tools-list.json params._meta.io.modelcontextprotocol/clientCapabilities=-)"
```
```
{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"params._meta is missing the required envelope key(s): io.modelcontextprotocol/protocolVersion"}}
HTTP 400
{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"params._meta is missing the required envelope key(s): io.modelcontextprotocol/clientCapabilities"}}
HTTP 400
```

**1.6 Resource inexistente: -32602**

```bash
mcp_post "$(corpo 05-resources-read-politica.json params.uri=politica://inexistente)"
```
```
{"jsonrpc":"2.0","id":5,"error":{"code":-32602,"message":"Unknown resource: politica://inexistente","data":{"uri":"politica://inexistente"}}}
HTTP 400
```

**1.7 Tool inexistente é recusada** (o SDK usa `isError`, forma aceita pelo enunciado)

```bash
mcp_post "$(corpo 02-tools-call-livre.json params.name=voar_delorean params.arguments={})"
```
```
{"jsonrpc":"2.0","id":2,"result":{"content":[{"text":"Unknown tool: voar_delorean","type":"text"}],"isError":true,"resultType":"complete","_meta":{"io.modelcontextprotocol/serverInfo":{"name":"central-de-salas","version":"1.0.0"}}}}
HTTP 200
```

**1.8 Header que não bate com o corpo: -32020**

```bash
curl -s $MCP/mcp -w '\nHTTP %{http_code}\n' -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/list' -d "$(corpo 02-tools-call-livre.json)"
```
```
{"jsonrpc":"2.0","id":2,"error":{"code":-32020,"message":"mcp-method header does not match the request body's method"}}
HTTP 400
```

**No terminal 1**, cada request aparece numa linha com método, id, nome, capability de elicitation e traceparent:

```
[mcp] method=tools/list id=1 elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
```

---

## 2. Política de uso e erros de execução

Continua no mesmo servidor. `c SALA INICIO FIM` chama `consultar_disponibilidade` no dia 2026-11-03:

```bash
c() { mcp_post "$(corpo 02-tools-call-livre.json params.name=consultar_disponibilidade "params.arguments={\"sala\":\"$1\",\"inicio\":\"2026-11-03T$2:00-03:00\",\"fim\":\"2026-11-03T$3:00-03:00\"}")"; }

c sala-aquario 09:00 10:00 | pegar result.structuredContent   # 2.1 livre
c sala-garagem 14:00 15:00 | pegar result.structuredContent   # 2.2 ocupada
c sala-delorean 09:00 10:00 | pegar result.content.0.text     # 2.3 sala inexistente
c sala-aquario 07:00 08:00 | pegar result.content.0.text      # 2.4 fora da janela
c sala-aquario 09:00 12:00 | pegar result.content.0.text      # 2.5 mais de 2 horas
c sala-aquario 10:00 09:00 | pegar result.content.0.text      # 2.6 intervalo invertido
```
```
{"sala": "sala-aquario", "livre": true, "conflitos": []}
{"sala": "sala-garagem", "livre": false, "conflitos": [{"id": "res-0001", "inicio": "2026-11-03T14:00:00-03:00", "fim": "2026-11-03T15:00:00-03:00", "responsavel": "Marty"}]}
Error executing tool consultar_disponibilidade: Sala inexistente: sala-delorean
Error executing tool consultar_disponibilidade: Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00
Error executing tool consultar_disponibilidade: Duracao acima do limite: a politica permite no maximo 2 horas
Error executing tool consultar_disponibilidade: Intervalo invalido: fim deve ser posterior a inicio
```
Os erros de 2.3 a 2.6 vêm com `isError: true` (veja sem o `pegar`). `reservar_sala` aplica as mesmas validações.

---

## 3. MRTR na reserva

Continua no mesmo servidor, ainda sem reiniciar.

**3.1 Reserva livre: `complete` direto** (`exemplos/wire/02`)

```bash
mcp_post "$(corpo 02-tools-call-livre.json)" | pegar result.structuredContent
```
```
{"reserva": "res-0003", "reservado": true, "sala": "sala-aquario", "inicio": "2026-11-03T09:00:00-03:00", "fim": "2026-11-03T10:00:00-03:00", "responsavel": "Doc", "politica": "2026-11-01", "motivo": null}
```

**3.2 Conflito: `input_required` com uma elicitation em form mode e um `requestState`** (`exemplos/wire/03`)

```bash
mcp_post "$(corpo 03-tools-call-conflito-input-required.json)" > /tmp/conflito.txt
pegar result.resultType < /tmp/conflito.txt
pegar result.inputRequests < /tmp/conflito.txt
ESTADO=$(pegar result.requestState < /tmp/conflito.txt); echo "${ESTADO:0:24}..."
CHAVE='__main__:escolha_de_sala'
ACEITE="params.inputResponses={\"$CHAVE\":{\"action\":\"accept\",\"content\":{\"sala\":\"sala-fusca\"}}}"
```
```
input_required
{"__main__:escolha_de_sala": {"method": "elicitation/create", "params": {"message": "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", "mode": "form", "requestedSchema": {"properties": {"sala": {"description": "Sala alternativa escolhida", "enum": ["sala-fusca", "sala-mirante"], "title": "Sala", "type": "string"}}, "required": ["sala"], "type": "object"}}}}
v1.U8D7eN_S7WOlgItV7u-7e...
```
O enum traz as salas livres com capacidade ≥ 12, por capacidade e id, e no máximo três.

**3.3 O mesmo conflito sem declarar elicitation: -32021 e HTTP 400** (`exemplos/wire/06`)

```bash
mcp_post "$(corpo 06-erro-32021-sem-elicitation.json)"
```
```
{"jsonrpc":"2.0","id":6,"error":{"code":-32021,"message":"Client did not declare the form elicitation capability required by resolver '__main__:escolha_de_sala'","data":{"requiredCapabilities":{"elicitation":{"form":{}}}}}}
HTTP 400
```

**3.4 `requestState` com um caractere trocado: -32602**

```bash
ADULTERADO="${ESTADO:0:40}$( [ "${ESTADO:40:1}" = A ] && echo B || echo A )${ESTADO:41}"
mcp_post "$(corpo 04-tools-call-retry.json params.requestState=$ADULTERADO "$ACEITE")"
```
```
{"jsonrpc":"2.0","id":4,"error":{"code":-32602,"message":"Invalid or expired requestState","data":{"reason":"invalid_request_state"}}}
HTTP 400
```
No terminal 1 aparece o motivo real: `requestState rejected on tools/call: seal`.

**3.5 Retry com argumentos adulterados (`responsavel=Biff`): rejeitado, não reserva**

```bash
mcp_post "$(corpo 04-tools-call-retry.json params.requestState=$ESTADO "$ACEITE" params.arguments.responsavel=Biff)"
```
```
{"jsonrpc":"2.0","id":4,"error":{"code":-32602,"message":"Invalid or expired requestState","data":{"reason":"invalid_request_state"}}}
HTTP 400
```
No terminal 1: `requestState rejected on tools/call: request binding`.

**3.6 Retry válido depois de reiniciar o servidor MCP** (`exemplos/wire/04`)

Reinicie o servidor no terminal 1 (Ctrl+C e `uv run python servidor.py`, com o mesmo segredo exportado). Depois:

```bash
mcp_post "$(corpo 04-tools-call-retry.json params.requestState=$ESTADO "$ACEITE")"
```
```
{"jsonrpc":"2.0","id":4,"result":{"content":[{"text":"{\n  \"reserva\": \"res-0003\",\n  \"reservado\": true,\n  \"sala\": \"sala-fusca\", ...}"}],"isError":false,"resultType":"complete","structuredContent":{"reserva":"res-0003","reservado":true,"sala":"sala-fusca","inicio":"2026-11-03T14:00:00-03:00","fim":"2026-11-03T15:00:00-03:00","responsavel":"Marty","politica":"2026-11-01","motivo":null},...}
HTTP 200
```
O processo novo não tinha nada em memória: tudo veio do `requestState` e dos argumentos que ele sela. O id do retry (4) é diferente do id do pedido (3).

**3.7 Recusa (`action: decline`): `complete`, sem `isError`, `reservado: false`** (`exemplos/wire/11`)

```bash
R=$(mcp_post "$(corpo 11-tools-call-retry-recusa.json params.inputResponses=- params.requestState=-)"); pegar result.resultType <<< "$R"
mcp_post "$(corpo 11-tools-call-retry-recusa.json params.requestState=$(pegar result.requestState <<< "$R"))" | pegar result.structuredContent
```
```
input_required
{"reserva": null, "reservado": false, "sala": null, "inicio": null, "fim": null, "responsavel": null, "politica": null, "motivo": "recusado"}
```

**3.8 Conflito sem alternativa possível: `isError`, sem elicitation**

```bash
r() { mcp_post "$(corpo 02-tools-call-livre.json 'params.arguments={"sala":"sala-mirante","inicio":"2026-11-03T11:00:00-03:00","fim":"2026-11-03T12:00:00-03:00","responsavel":"Doc"}')"; }
r | pegar result.structuredContent.reserva   # a primeira reserva passa
r | pegar result                             # a segunda conflita, e nao ha sala com capacidade >= 20
```
```
res-0004
{"content": [{"text": "Error executing tool reservar_sala: Sem alternativas disponiveis no intervalo", "type": "text"}], "isError": true, "resultType": "complete", ...}
```

**3.9 Expiração.** O `requestState` vale 10 minutos (`VALIDADE_REQUEST_STATE` em [servidor-mcp/servidor.py](servidor-mcp/servidor.py)). Para conferir, gere um conflito como em 3.2, espere mais de 10 minutos e reenvie o retry: a resposta é a mesma de 3.4, e o terminal 1 mostra `requestState rejected on tools/call: expired`.

---

## 4. O agente como host MCP

**Reinicie os dois processos** (terminal 1 e terminal 2). Depois:

```bash
P() { echo "reservar sala=$1 inicio=2026-11-03T$2:00-03:00 fim=2026-11-03T$3:00-03:00 responsavel=$4"; }
a2a_enviar "$(P sala-porao 09:00 10:00 Doc)" | resumo_task
```
```
task     : task-d495471baa28 | contexto: ctx-827aad500bec
estado   : TASK_STATE_COMPLETED
mensagem : Reserva res-0003 confirmada na sala-porao.
artifact : reserva {"reserva": "res-0003", "sala": "sala-porao", "inicio": "2026-11-03T09:00:00-03:00", "fim": "2026-11-03T10:00:00-03:00", "responsavel": "Doc", "politica": "2026-11-01"}
```

No **terminal 1** (servidor MCP), confira:

```
[mcp] method=tools/list id=1 elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-4c2b7e4dcdc2c617-01
[mcp] method=resources/read id=2 name=politica://uso elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-f362f91e9ad01d95-01
[mcp] method=tools/call id=3 name=reservar_sala elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-3f3a4c23856c99c9-01
```

- `tools/list` vem antes do primeiro `tools/call`: o agente descobre as tools em tempo de execução e monta os argumentos a partir do `inputSchema`.
- `resources/read` de `politica://uso`: a versão `2026-11-01` sai daqui e vai para o campo `politica` do artifact.
- `elicitation=form`: todo request do agente declara `{"elicitation": {"form": {}}}` no `_meta`.
- O traceparent tem o mesmo trace-id (`4bf92f35...`) do header enviado ao agente, com span-id novo em cada request.

No **terminal 2** (agente) aparecem as transições `SUBMITTED -> WORKING -> COMPLETED`.

---

## 5. O agente como servidor A2A

Continua nos mesmos processos da seção 4.

```bash
# 5.1 Agent Card
curl -s $AGENTE/.well-known/agent-card.json | python3 -c 'import json,sys; c=json.load(sys.stdin); print(c["name"], "|", json.dumps(c["supportedInterfaces"]), "|", [s["id"] for s in c["skills"]])'

# 5.2 sala livre: COMPLETED com artifact
R=$(a2a_enviar "$(P sala-aquario 10:00 11:00 Doc)"); resumo_task <<< "$R"; T=$(pegar result.task.id <<< "$R")

# 5.3 GetTask: id, contextId, estado corrente e artifact
a2a_get $T | python3 -c 'import json,sys; t=json.load(sys.stdin)["result"]["task"]; print(t["id"], t["contextId"], t["status"]["state"], [m["role"] for m in t["history"]], [a["name"] for a in t["artifacts"]])'

# 5.4 SendMessage para uma Task terminal: erro
a2a_enviar "escolha=sala-fusca" $T | resumo_task

# 5.5 sala inexistente: FAILED com a mensagem exata da tool
a2a_enviar "$(P sala-inexistente 09:00 10:00 Doc)" | resumo_task

# 5.6 pedido fora do formato fixo: REJECTED
a2a_enviar "quero uma sala amanha" | resumo_task

# 5.7 GetTask de Task inexistente
a2a_get task-nao-existe | resumo_task
```
```
Central de Salas | [{"url": "http://127.0.0.1:7300/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}] | ['reservar-sala']
task     : task-10f02f0c13a9 | contexto: ctx-bf1b7cf46304
estado   : TASK_STATE_COMPLETED
mensagem : Reserva res-0004 confirmada na sala-aquario.
artifact : reserva {"reserva": "res-0004", "sala": "sala-aquario", "inicio": "2026-11-03T10:00:00-03:00", "fim": "2026-11-03T11:00:00-03:00", "responsavel": "Doc", "politica": "2026-11-01"}
task-10f02f0c13a9 ctx-bf1b7cf46304 TASK_STATE_COMPLETED ['ROLE_USER', 'ROLE_AGENT'] ['reserva']
ERRO {"code": -32004, "message": "A Task task-10f02f0c13a9 ja terminou em TASK_STATE_COMPLETED"}
task     : task-5fe9aeab9c5a | contexto: ctx-2ec71d6b446a
estado   : TASK_STATE_FAILED
mensagem : Error executing tool reservar_sala: Sala inexistente: sala-inexistente
task     : task-911be093d9dc | contexto: ctx-66a3ed126025
estado   : TASK_STATE_REJECTED
mensagem : Pedido fora do formato. Use: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>
ERRO {"code": -32001, "message": "Task nao encontrada: task-nao-existe"}
```
Para comparar o card inteiro com `exemplos/wire/07-a2a-agent-card.json`: `curl -s $AGENTE/.well-known/agent-card.json | python3 -m json.tool`.

---

## 6. A ponte

Continua nos mesmos processos.

```bash
# 6.1 sala ocupada (exemplos/wire/08): a Task pausa com a linha de alternativas
R=$(a2a_post "$(corpo 08-a2a-send-message.json)"); resumo_task <<< "$R"; T=$(pegar result.task.id <<< "$R")

# 6.2 GetTask reflete a pausa
a2a_get $T | resumo_task

# 6.3 escolha fora do enum: continua pausada e repete a lista
a2a_enviar "escolha=sala-aquario" $T | resumo_task

# 6.4 escolha valida (exemplos/wire/10): retry MCP com id novo, COMPLETED na sala escolhida
a2a_enviar "escolha=sala-mirante" $T | resumo_task
```
```
task     : task-7e9ea07d0c92 | contexto: ctx-40e1634c9057
estado   : TASK_STATE_INPUT_REQUIRED
mensagem : alternativas: sala-fusca, sala-mirante
task     : task-7e9ea07d0c92 | contexto: ctx-40e1634c9057
estado   : TASK_STATE_INPUT_REQUIRED
mensagem : alternativas: sala-fusca, sala-mirante
task     : task-7e9ea07d0c92 | contexto: ctx-40e1634c9057
estado   : TASK_STATE_INPUT_REQUIRED
mensagem : alternativas: sala-fusca, sala-mirante
task     : task-7e9ea07d0c92 | contexto: ctx-40e1634c9057
estado   : TASK_STATE_COMPLETED
mensagem : Reserva res-0005 confirmada na sala-mirante.
artifact : reserva {"reserva": "res-0005", "sala": "sala-mirante", "inicio": "2026-11-03T14:00:00-03:00", "fim": "2026-11-03T15:00:00-03:00", "responsavel": "Marty", "politica": "2026-11-01"}
```

No **terminal 1**, o par da pausa: o `tools/call` inicial e o retry, com ids diferentes e o mesmo trace-id. A escolha inválida de 6.3 não gerou request MCP: o agente validou contra o enum que já tinha.

```
[mcp] method=tools/call id=12 name=reservar_sala elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-ad8d6af9c2f5232a-01
[mcp] method=tools/call id=13 name=reservar_sala retry=sim elicitation=form traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-4eb10c9a0e92acb6-01
```

```bash
# 6.5 o mesmo pedido de novo e recusa: CANCELED (a sala-mirante ja foi reservada em 6.4)
R=$(a2a_post "$(corpo 08-a2a-send-message.json)"); resumo_task <<< "$R"; T=$(pegar result.task.id <<< "$R")
a2a_enviar "escolha=recusar" $T | resumo_task
```
```
task     : task-ca43fdfce8a3 | contexto: ctx-fb415795e683
estado   : TASK_STATE_INPUT_REQUIRED
mensagem : alternativas: sala-fusca
task     : task-ca43fdfce8a3 | contexto: ctx-fb415795e683
estado   : TASK_STATE_CANCELED
mensagem : Reserva nao realizada: recusado.
```

```bash
# 6.6 duas Tasks pausadas ao mesmo tempo, retomadas em ordem inversa
A=$(a2a_enviar "$(P sala-fusca 16:00 17:00 Lorraine)" | pegar result.task)
B=$(a2a_enviar "$(P sala-garagem 14:00 15:00 George)" | pegar result.task)
for X in "$A" "$B"; do python3 -c 'import json,sys; t=json.loads(sys.argv[1]); print(t["id"], t["status"]["state"], t["status"]["message"]["parts"][0]["text"])' "$X"; done
TA=$(pegar id <<< "$A"); TB=$(pegar id <<< "$B")
a2a_enviar "escolha=sala-fusca" $TB | resumo_task
a2a_enviar "escolha=sala-garagem" $TA | resumo_task
```
```
task-d607260a659a TASK_STATE_INPUT_REQUIRED alternativas: sala-garagem, sala-mirante
task-04cadac2cdc8 TASK_STATE_INPUT_REQUIRED alternativas: sala-fusca
task     : task-04cadac2cdc8 | contexto: ctx-07a5e0ce6567
estado   : TASK_STATE_COMPLETED
mensagem : Reserva res-0006 confirmada na sala-fusca.
artifact : reserva {"reserva": "res-0006", "sala": "sala-fusca", "inicio": "2026-11-03T14:00:00-03:00", "fim": "2026-11-03T15:00:00-03:00", "responsavel": "George", "politica": "2026-11-01"}
task     : task-d607260a659a | contexto: ctx-ed8f5275f562
estado   : TASK_STATE_COMPLETED
mensagem : Reserva res-0007 confirmada na sala-garagem.
artifact : reserva {"reserva": "res-0007", "sala": "sala-garagem", "inicio": "2026-11-03T16:00:00-03:00", "fim": "2026-11-03T17:00:00-03:00", "responsavel": "Lorraine", "politica": "2026-11-01"}
```
Cada Task concluiu com o próprio intervalo e o próprio responsável: os `requestState` não se misturaram.

```bash
# 6.7 o requestState (prefixo "v1.") nao aparece em nenhuma resposta A2A
for t in $TA $TB $T; do a2a_get $t; done | grep -c '"v1\.'
```
```
0
```

---

## 7. Validador

**Reinicie os dois processos** e rode na raiz:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301; echo "exit=$?"
```

O esperado é `resumo: 36 passaram, 0 falharam, de 36 verificacoes` e `exit=0`. A saída completa da última execução está no [README](README.md#saída-do-validador). Depois, procure no terminal 1 o trace-id que o validador imprimiu na primeira linha: ele aparece no `traceparent` do `tools/list`, do `resources/read` e dos `tools/call` emitidos pelo agente.
