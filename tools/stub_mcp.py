"""Stub stdio MCP server for testing the IDE's MCP client plumbing."""
import json
import sys


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            send(rid, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                       "serverInfo": {"name": "stub", "version": "0.1"}})
        elif method == "tools/list":
            send(rid, {"tools": [{
                "name": "echo",
                "description": "Echo back the input text.",
                "inputSchema": {"type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"]},
            }]})
        elif method == "tools/call":
            args = (msg.get("params") or {}).get("arguments") or {}
            send(rid, {"content": [{"type": "text",
                                    "text": f"echo: {args.get('text', '')}"}]})
        elif rid is not None and method:
            send(rid, {})


def send(rid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
