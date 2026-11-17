"""Minimal stdio LSP server used to test the IDE's LSP plumbing.

Speaks just enough of the protocol for initialize / didOpen / hover /
completion / publishDiagnostics. Not a real language server.
"""
import json
import os
import sys


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.decode().strip().lower()] = v.decode().strip()
    length = int(headers.get("content-length") or 0)
    if not length:
        return None
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send(msg):
    body = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()


def main():
    opened = {}
    while True:
        msg = read_message()
        if msg is None:
            break
        method = msg.get("method")
        params = msg.get("params") or {}
        rid = msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {
                "hoverProvider": True,
                "completionProvider": {"triggerCharacters": ["."]},
                "definitionProvider": True,
                "textDocumentSync": 1,
            }}})
        elif method == "initialized":
            pass
        elif method == "textDocument/didOpen":
            td = params.get("textDocument") or {}
            opened[td.get("uri")] = td.get("text") or ""
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                  "params": {"uri": td.get("uri"), "diagnostics": [{
                      "range": {"start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 4}},
                      "severity": 2,
                      "source": "stub",
                      "message": "stub diagnostic",
                  }]}})
        elif method == "textDocument/didChange":
            td = (params.get("textDocument") or {})
            changes = params.get("contentChanges") or [{}]
            opened[td.get("uri")] = changes[0].get("text") or ""
        elif method == "textDocument/hover":
            uri = (params.get("textDocument") or {}).get("uri")
            text = opened.get(uri, "")
            first = text.splitlines()[0][:40] if text else "(empty)"
            send({"jsonrpc": "2.0", "id": rid, "result": {"contents": {
                "kind": "markdown", "value": f"**stub hover** · `{first}`"}}})
        elif method == "textDocument/definition":
            uri = (params.get("textDocument") or {}).get("uri")
            send({"jsonrpc": "2.0", "id": rid,
                  "result": [{"uri": uri,
                              "range": {"start": {"line": 0, "character": 0},
                                        "end": {"line": 0, "character": 4}}}]})
        elif method == "textDocument/completion":
            send({"jsonrpc": "2.0", "id": rid, "result": [
                {"label": "stub_one", "kind": 2, "detail": "from stub lsp"},
                {"label": "stub_two", "kind": 2, "detail": "from stub lsp"},
            ]})
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": rid, "result": None})
        elif method == "exit":
            break


if __name__ == "__main__":
    main()
