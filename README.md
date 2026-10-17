# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio "A Ponte" (Hill Valley Tech, cinco salas de reunião). São dois processos Python separados:

| Processo | Pasta | Porta | Endpoint | Papel |
|---|---|---|---|---|
| Servidor MCP `central-de-salas` | [servidor-mcp/](servidor-mcp/) | 7301 | `POST /mcp` (Streamable HTTP, revisão 2026-07-28, stateless) | 3 tools, 1 resource, MRTR na reserva |
| Agente `Central de Salas` | [agente/](agente/) | 7300 | `POST /a2a` (A2A v1.0, JSON-RPC 2.0) e `GET /.well-known/agent-card.json` | Host MCP por dentro, servidor A2A por fora |

Stack: Python 3.12 com [uv](https://docs.astral.sh/uv/), SDK oficial `mcp==2.3.0` (v2, alinhado à revisão 2026-07-28), `starlette==1.8.0` e `uvicorn==0.54.0`. As versões estão travadas em cada `pyproject.toml` e nos `uv.lock` commitados. O agente não usa LLM e decide por regra.

O roteiro de testes manuais, passo a passo e com a saída esperada, está em [TESTES-MANUAIS.md](TESTES-MANUAIS.md).

## Como rodar

Pré-requisitos: `git`, `python3` (3.10 ou superior, para o validador) e `uv` (instalação: `curl -LsSf https://astral.sh/uv/install.sh | sh`). O `uv` baixa o Python 3.12 dos projetos se ele não estiver instalado.

```bash
git clone https://github.com/KaduMelo/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
```

**Terminal 1, servidor MCP.** Precisa de `REQUEST_STATE_SECRET` com pelo menos 32 bytes de aleatoriedade. Gere a sua e não commite o valor:

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
cd servidor-mcp
uv run python servidor.py
```

Sem o segredo, ou com menos de 32 bytes, o processo se recusa a subir e mostra o comando acima. Para testar o retry depois de um restart, reinicie o servidor no mesmo terminal, com o mesmo `REQUEST_STATE_SECRET` exportado: um `requestState` selado com outra chave é rejeitado.

**Terminal 2, agente:**

```bash
cd agente
uv run python agente.py
```

**Terminal 3, validador** (na raiz do repositório, com os dois processos recém-iniciados):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

O stderr do servidor MCP (terminal 1) registra uma linha por request, com método, id, nome da tool ou do resource e traceparent. O retry de uma reserva pausada aparece marcado com `retry=sim` e tem id diferente do request inicial:

```
[mcp] method=tools/list id=1 traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-b6ab53c7be477614-01
[mcp] method=resources/read id=2 name=politica://uso traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-9d3b1c6b0b438e04-01
[mcp] method=tools/call id=3 name=reservar_sala traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-a4dc65fe37a23e91-01
[mcp] method=tools/call id=4 name=reservar_sala retry=sim traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-d86bee743d8e8948-01
```

O stderr do agente (terminal 2) registra cada request A2A e cada transição de estado das Tasks.

Variáveis de ambiente opcionais, com os padrões do enunciado:

| Variável | Padrão | Processo |
|---|---|---|
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `7301` | servidor MCP |
| `AGENTE_HOST` / `AGENTE_PORT` | `127.0.0.1` / `7300` | agente |
| `AGENTE_URL` | `http://127.0.0.1:7300` | URL pública publicada no Agent Card |
| `MCP_URL` | `http://127.0.0.1:7301/mcp` | servidor MCP que o agente consome |

## Onde a ponte acontece

A ponte inteira fica em [agente/agente.py](agente/agente.py), em dois pontos marcados com o comentário `PONTE`.

**Ida, do `input_required` à pausa.** Em [agente/agente.py:184-192](agente/agente.py#L184-L192), `concluir_com` recebe o resultado do `tools/call`. Quando ele é um `InputRequiredResult`, `pergunta_de` ([agente/host_mcp.py:125](agente/host_mcp.py#L125)) extrai a chave do `inputRequests`, o campo e o `enum` da elicitation em form mode. A Task guarda em `task.pausa` a tool, os argumentos, essa chave e o `requestState` sem abri-lo, e vai para `TASK_STATE_INPUT_REQUIRED` com a mensagem `alternativas: <ids>`, na ordem do enum. O `requestState` chega cru porque o host MCP chama `session.call_tool(..., allow_input_required=True)` ([agente/host_mcp.py:121](agente/host_mcp.py#L121)) e não registra callback de elicitation, então o SDK não fecha o ciclo sozinho.

**Volta, do `escolha=` ao retry.** Em [agente/agente.py:241-263](agente/agente.py#L241-L263), `retomar` traduz `escolha=<id>` em `action: accept` e `escolha=recusar` em `action: decline`. Uma escolha fora do enum mantém a Task pausada e repete a lista. Depois disso, o agente reemite o mesmo `tools/call`, levando `inputResponses` com a mesma chave recebida e o `requestState` ecoado byte a byte. O SDK gera um id JSON-RPC novo em cada chamada, o que dá ids diferentes para o pedido inicial e o retry, como mostra o log acima. O resultado volta para `concluir_com`, que leva a Task a `COMPLETED` (com o artifact `reserva`), `CANCELED` (recusa) ou `FAILED` (`isError` ou erro de protocolo).

Do lado do servidor, o par da ponte é o resolver `escolha_de_sala` ([servidor-mcp/servidor.py:200](servidor-mcp/servidor.py#L200)), ligado ao parâmetro `escolha` de `reservar_sala` por `Annotated[..., Resolve(escolha_de_sala)]`. Quando a sala está ocupada, ele devolve um `Elicit` com o schema `{sala: enum[alternativas]}`. A partir daí, o SDK cuida do resto: responde `resultType: input_required`, sela o `requestState`, recusa com -32021 o cliente sem `elicitation.form` e, no retry, valida o estado e injeta a resposta. Não há canal de volta nem chamada aberta esperando o usuário.

## Decisões técnicas

**Proteção do `requestState`.** Uso o utilitário do SDK, `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600)` ([servidor-mcp/servidor.py:184](servidor-mcp/servidor.py#L184)), instalado pelo `MCPServer` como middleware de fronteira. Ele faz o seguinte:

- O token é AES-256-GCM (AEAD) com chave derivada por HKDF-SHA256 do segredo, no formato `v1.<base64url>`. Ele é cifrado e autenticado, então trocar qualquer caractere quebra a tag GCM.
- O envelope selado carrega `iat` e `exp`, o método, o nome da tool, um digest SHA-256 dos argumentos originais e a audiência `central-de-salas`.
- A verificação recusa token adulterado, expirado, emitido para outra tool, com argumentos divergentes ou de outra audiência, sempre com `-32602 Invalid or expired requestState` e HTTP 400. O motivo real vai só para o log.
- Argumentos adulterados no retry caem no caminho "o servidor rejeita o estado".
- O segredo vem só da variável de ambiente. O processo não sobe sem ela, e o SDK exige pelo menos 32 bytes.

**Validade.** São 10 minutos (`VALIDADE_REQUEST_STATE = 10 * 60`), dentro da faixa de 5 a 30 pedida no enunciado. Testei com uma instância do servidor com o relógio do verificador adiantado 11 minutos: o mesmo token foi rejeitado com -32602, e o log registrou `requestState rejected on tools/call: expired`. Com o relógio normal, o retry concluiu.

**Nada guardado entre os rounds.** O servidor não mantém estado entre o `input_required` e o retry. No retry, o resolver roda de novo a partir dos argumentos, e o SDK só aceita a resposta se a pergunta renderizada for idêntica à que foi selada no estado (o digest da pergunta viaja no `requestState`). Como a chave vem do ambiente, um retry depois de reiniciar o processo funciona. As reservas ficam em memória e não sobrevivem ao restart, como o enunciado permite.

**Estado das Tasks.** Fica em memória no processo do agente, no dicionário `TASKS` de [agente/agente.py](agente/agente.py), com um `asyncio.Lock` por Task. O estado pausado (`Pausa`: tool, argumentos, chave, campo, alternativas e `requestState`) é por Task, e por isso duas Tasks pausadas ao mesmo tempo não trocam de estado. `Task.para_json()` nunca serializa a `Pausa`, então o `requestState` não aparece no card, no artifact, no histórico nem nas mensagens. Estados terminais (`COMPLETED`, `CANCELED`, `FAILED`, `REJECTED`) são definitivos: um `SendMessage` para uma Task terminal recebe o erro `-32004` (UnsupportedOperation), e uma Task inexistente recebe `-32001` (TaskNotFound).

**Host MCP.** O agente mantém um único `mcp.Client` vivo, com a versão fixada em `2026-07-28`, e por isso não faz `initialize` nem `server/discover`. Todo request carrega no próprio `_meta` a versão, o `clientInfo` e as capabilities, e o SDK espelha `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` nos headers. Em cada Task, o agente:

1. faz `tools/list` e monta os argumentos a partir do `inputSchema` descoberto, sem lista fixa;
2. lê `politica://uso` e extrai a `versao` da primeira linha, que vai no artifact;
3. chama `reservar_sala`.

O trace-id do header `traceparent` do cliente A2A é propagado no `_meta.traceparent` de todos os requests MCP da Task, com span-id novo em cada um. Sem o header, o agente gera um trace-id por Task.

**A2A sem SDK de A2A.** O binding JSON-RPC (`SendMessage`, `GetTask`) e o Agent Card foram escritos à mão sobre Starlette, seguindo `exemplos/wire/07` a `10`, porque o binding é pequeno e assim o formato fica exatamente o do contrato. Um pedido fora do formato fixo termina em `TASK_STATE_REJECTED`.

**Limitações do SDK, documentadas em vez de contornadas:**

- **Capability de elicitation.** O `ClientSession` do SDK só anuncia elicitation quando recebe um `elicitation_callback`. Nesse caso anuncia `form` e `url` juntos e passa a responder as perguntas sozinho (`mcp/client/session.py`, `_build_capabilities`):
  ```python
  elicitation = (
      types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
      if self._elicitation_callback is not _default_elicitation_callback
      else None
  )
  ```
  Para declarar exatamente `{"elicitation": {"form": {}}}` sem callback, o agente usa uma subclasse da sessão que sobrescreve só `_build_capabilities` ([agente/host_mcp.py:29](agente/host_mcp.py#L29)). O resto do protocolo continua sendo o do SDK.
- **Tool inexistente.** O `MCPServer` do SDK recusa a chamada com `isError: true` (`Unknown tool: ...`) em vez de -32602, forma que o enunciado aceita.
- **Chave do `inputRequests`.** O SDK gera a chave a partir de `módulo:qualname` do resolver. Como o servidor roda como script, ela fica `__main__:escolha_de_sala`, igual à de `exemplos/wire/03`.

## Saída do validador

Última execução, num clone limpo, com os dois processos recém-iniciados:

```
<!-- SAIDA-DO-VALIDADOR -->
```
