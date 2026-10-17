# Funcoes de apoio para os testes manuais. Uso: source ./testes.sh
# Dependem so de curl e python3 (o mesmo python3 do validador), sem jq.

AGENTE=${AGENTE:-http://localhost:7300}
MCP=${MCP:-http://localhost:7301}
TP=${TP:-00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01}
WIRE=${WIRE:-exemplos/wire}

# corpo ARQUIVO [caminho=valor ...]
#   Imprime request.body de exemplos/wire/ARQUIVO, com campos trocados.
#   caminho usa pontos (params.message.taskId); depois de params._meta. o resto e uma chave so
#   (params._meta.io.modelcontextprotocol/protocolVersion). valor e JSON, ou texto se nao for JSON.
#   caminho=- remove o campo.
corpo() {
  python3 - "$WIRE/$1" "${@:2}" <<'PY'
import json, sys
corpo = json.load(open(sys.argv[1]))["request"]["body"]
for troca in sys.argv[2:]:
    caminho, _, valor = troca.partition("=")
    if caminho.startswith("params._meta."):  # chaves do _meta tem pontos no nome
        pais, folha = ["params", "_meta"], caminho[len("params._meta."):]
    else:
        *pais, folha = caminho.split(".")
    alvo = corpo
    for p in pais:
        alvo = alvo.setdefault(p, {})
    if valor == "-":
        alvo.pop(folha, None)
        continue
    try:
        alvo[folha] = json.loads(valor)
    except ValueError:
        alvo[folha] = valor
print(json.dumps(corpo))
PY
}

# mcp_post CORPO  -> POST no servidor MCP com os headers espelhados do corpo; imprime JSON e o status HTTP
mcp_post() {
  local metodo nome
  metodo=$(printf '%s' "$1" | pegar method)
  nome=$(printf '%s' "$1" | python3 -c 'import json,sys; p=json.load(sys.stdin).get("params",{}); print(p.get("name") or p.get("uri") or "")')
  curl -s "$MCP/mcp" -w '\nHTTP %{http_code}\n' \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H 'MCP-Protocol-Version: 2026-07-28' -H "Mcp-Method: $metodo" ${nome:+-H "Mcp-Name: $nome"} \
    -d "$1"
}

# a2a_post CORPO  -> POST JSON-RPC no agente, com o header traceparent de $TP
a2a_post() {
  curl -s "$AGENTE/a2a" -H 'Content-Type: application/json' -H "traceparent: $TP" -d "$1"
}

# a2a_enviar TEXTO [TASK_ID]  -> SendMessage com o texto dado (e a Task, se for continuacao)
a2a_enviar() {
  local msg
  msg=$(python3 -c 'import json,sys,secrets
m={"messageId":"msg-"+secrets.token_hex(6),"role":"ROLE_USER","parts":[{"text":sys.argv[1]}]}
if len(sys.argv)>2 and sys.argv[2]: m["taskId"]=sys.argv[2]
print(json.dumps({"jsonrpc":"2.0","id":secrets.token_hex(4),"method":"SendMessage","params":{"message":m}}))' "$1" "${2:-}")
  a2a_post "$msg"
}

# a2a_get TASK_ID
a2a_get() {
  a2a_post "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"GetTask\",\"params\":{\"id\":\"$1\"}}"
}

# pegar CAMINHO  -> le JSON da entrada (ignora a linha "HTTP nnn") e imprime o valor no caminho com pontos
pegar() {
  python3 -c 'import json,sys
texto=sys.stdin.read().split("\nHTTP ")[0]
d=json.loads(texto)
for k in sys.argv[1].split("."):
    if k: d=d[int(k)] if isinstance(d,list) else d[k]
print(d if isinstance(d,str) else json.dumps(d, ensure_ascii=False))' "$1"
}

# resumo_task  -> mostra id, estado, mensagem de status e artifact de uma resposta A2A
resumo_task() {
  python3 -c 'import json,sys
r=json.load(sys.stdin)
if "error" in r: print("ERRO", json.dumps(r["error"], ensure_ascii=False)); sys.exit()
t=r["result"].get("task", r["result"])
msg=" ".join(p.get("text","") for p in (t["status"].get("message") or {}).get("parts",[]))
print("task     :", t["id"], "| contexto:", t["contextId"])
print("estado   :", t["status"]["state"])
print("mensagem :", msg)
for a in t.get("artifacts") or []:
    print("artifact :", a["name"], " ".join(p.get("text","") for p in a["parts"]))'
}
