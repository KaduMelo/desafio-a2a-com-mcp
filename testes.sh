# Helper functions for the manual tests. Usage: source ./testes.sh
# They only need curl and python3 (the same python3 as the validator), no jq.

AGENTE=${AGENTE:-http://localhost:7300}
MCP=${MCP:-http://localhost:7301}
TP=${TP:-00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01}
WIRE=${WIRE:-exemplos/wire}

# corpo FILE [path=value ...]
#   Prints request.body of exemplos/wire/FILE, with fields replaced.
#   path uses dots (params.message.taskId); after params._meta. the rest is a single key
#   (params._meta.io.modelcontextprotocol/protocolVersion). value is JSON, or plain text if not JSON.
#   path=- removes the field.
corpo() {
  python3 - "$WIRE/$1" "${@:2}" <<'PY'
import json, sys
corpo = json.load(open(sys.argv[1]))["request"]["body"]
for troca in sys.argv[2:]:
    caminho, _, valor = troca.partition("=")
    if caminho.startswith("params._meta."):  # _meta keys contain dots
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

# mcp_post BODY  -> POST to the MCP server with headers mirrored from the body; prints the JSON and the HTTP status
mcp_post() {
  local metodo nome
  metodo=$(printf '%s' "$1" | pegar method)
  nome=$(printf '%s' "$1" | python3 -c 'import json,sys; p=json.load(sys.stdin).get("params",{}); print(p.get("name") or p.get("uri") or "")')
  curl -s "$MCP/mcp" -w '\nHTTP %{http_code}\n' \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H 'MCP-Protocol-Version: 2026-07-28' -H "Mcp-Method: $metodo" ${nome:+-H "Mcp-Name: $nome"} \
    -d "$1"
}

# a2a_post BODY  -> JSON-RPC POST to the agent, with the traceparent header from $TP
a2a_post() {
  curl -s "$AGENTE/a2a" -H 'Content-Type: application/json' -H "traceparent: $TP" -d "$1"
}

# a2a_enviar TEXT [TASK_ID]  -> SendMessage with the given text (and the Task, when continuing one)
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

# pegar PATH  -> reads JSON from stdin (ignoring the "HTTP nnn" line) and prints the value at the dotted path
pegar() {
  python3 -c 'import json,sys
texto=sys.stdin.read().split("\nHTTP ")[0]
d=json.loads(texto)
for k in sys.argv[1].split("."):
    if k: d=d[int(k)] if isinstance(d,list) else d[k]
print(d if isinstance(d,str) else json.dumps(d, ensure_ascii=False))' "$1"
}

# resumo_task  -> shows the id, state, status message and artifact of an A2A response
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
