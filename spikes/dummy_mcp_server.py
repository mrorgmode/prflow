#!/usr/bin/env python3
"""Harmless stdio MCP server used only as a positive control in Spike C.

Speaks newline-delimited JSON-RPC: `initialize`, `tools/list` (one no-op tool),
`tools/call` (echoes), ignores notifications. It never touches the network.
"""

from __future__ import annotations

import json
import sys

TOOL = {
    "name": "prflow_dummy_echo",
    "description": "Phase 0 positive-control tool; echoes its input.",
    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if "id" not in msg:
            continue  # notification
        method = msg.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "prflow-dummy", "version": "0"},
            }
        elif method == "tools/list":
            result = {"tools": [TOOL]}
        elif method == "tools/call":
            text = str(msg.get("params", {}).get("arguments", {}).get("text", ""))
            result = {"content": [{"type": "text", "text": text}]}
        elif method == "ping":
            result = {}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not found"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
