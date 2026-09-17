"""Minimal JSON-RPC client for the owner's ha-mcp endpoint (streamable HTTP, SSE replies)."""
import json, os, sys, urllib.request

URL = os.environ["HA_MCP_URL"]
_sid = None
_id = 0

def _post(payload, want_result=True):
    global _sid
    hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if _sid:
        hdr["Mcp-Session-Id"] = _sid
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=hdr)
    with urllib.request.urlopen(req, timeout=120) as r:
        _sid = r.headers.get("Mcp-Session-Id") or _sid
        body = r.read().decode()
    if not want_result:
        return None
    if body.lstrip().startswith("{"):
        return json.loads(body)
    for line in body.splitlines():
        if line.startswith("data:"):
            d = json.loads(line[5:])
            if "result" in d or "error" in d:
                return d
    raise RuntimeError("no result in reply: " + body[:400])

def init():
    global _id
    _id += 1
    r = _post({"jsonrpc": "2.0", "id": _id, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "scorecard", "version": "0"}}})
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, want_result=False)
    return r

def call(tool, **args):
    global _id
    _id += 1
    r = _post({"jsonrpc": "2.0", "id": _id, "method": "tools/call",
               "params": {"name": tool, "arguments": args}})
    if "error" in r:
        raise RuntimeError(r["error"])
    out = r["result"]
    if out.get("structuredContent"):
        return out["structuredContent"]
    txt = out["content"][0]["text"]
    try:
        return json.loads(txt)
    except Exception:
        return txt

def tools():
    global _id
    _id += 1
    return _post({"jsonrpc": "2.0", "id": _id, "method": "tools/list"})["result"]["tools"]

if __name__ == "__main__":
    print(json.dumps(init())[:300])
    for t in tools():
        print(t["name"])
