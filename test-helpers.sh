# Helper functions for the manual tests. Usage: source ./test-helpers.sh
# They only need curl and python3 (the same python3 as the validator), no jq.

AGENT=${AGENT:-http://localhost:7300}
MCP=${MCP:-http://localhost:7301}
TP=${TP:-00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01}
WIRE=${WIRE:-exemplos/wire}

# body FILE [path=value ...]
#   Prints request.body of exemplos/wire/FILE, with fields replaced.
#   path uses dots (params.message.taskId); after params._meta. the rest is a single key
#   (params._meta.io.modelcontextprotocol/protocolVersion). value is JSON, or plain text if not JSON.
#   path=- removes the field.
body() {
  python3 - "$WIRE/$1" "${@:2}" <<'PY'
import json, sys
body = json.load(open(sys.argv[1]))["request"]["body"]
for change in sys.argv[2:]:
    path, _, value = change.partition("=")
    if path.startswith("params._meta."):  # _meta keys contain dots
        parents, leaf = ["params", "_meta"], path[len("params._meta."):]
    else:
        *parents, leaf = path.split(".")
    target = body
    for p in parents:
        target = target.setdefault(p, {})
    if value == "-":
        target.pop(leaf, None)
        continue
    try:
        target[leaf] = json.loads(value)
    except ValueError:
        target[leaf] = value
print(json.dumps(body))
PY
}

# mcp_post BODY  -> POST to the MCP server with headers mirrored from the body; prints the JSON and the HTTP status
mcp_post() {
  local method name
  method=$(printf '%s' "$1" | get_field method)
  name=$(printf '%s' "$1" | python3 -c 'import json,sys; p=json.load(sys.stdin).get("params",{}); print(p.get("name") or p.get("uri") or "")')
  curl -s "$MCP/mcp" -w '\nHTTP %{http_code}\n' \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H 'MCP-Protocol-Version: 2026-07-28' -H "Mcp-Method: $method" ${name:+-H "Mcp-Name: $name"} \
    -d "$1"
}

# a2a_post BODY  -> JSON-RPC POST to the agent, with the traceparent header from $TP
a2a_post() {
  curl -s "$AGENT/a2a" -H 'Content-Type: application/json' -H "traceparent: $TP" -d "$1"
}

# a2a_send TEXT [TASK_ID]  -> SendMessage with the given text (and the Task, when continuing one)
a2a_send() {
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

# get_field PATH  -> reads JSON from stdin (ignoring the "HTTP nnn" line) and prints the value at the dotted path
get_field() {
  python3 -c 'import json,sys
text=sys.stdin.read().split("\nHTTP ")[0]
d=json.loads(text)
for k in sys.argv[1].split("."):
    if k: d=d[int(k)] if isinstance(d,list) else d[k]
print(d if isinstance(d,str) else json.dumps(d, ensure_ascii=False))' "$1"
}

# task_summary  -> shows the id, state, status message and artifact of an A2A response
task_summary() {
  python3 -c 'import json,sys
r=json.load(sys.stdin)
if "error" in r: print("ERROR", json.dumps(r["error"], ensure_ascii=False)); sys.exit()
t=r["result"].get("task", r["result"])
msg=" ".join(p.get("text","") for p in (t["status"].get("message") or {}).get("parts",[]))
print("task     :", t["id"], "| context:", t["contextId"])
print("state    :", t["status"]["state"])
print("message  :", msg)
for a in t.get("artifacts") or []:
    print("artifact :", a["name"], " ".join(p.get("text","") for p in a["parts"]))'
}
