"""Tiny fake backend used only by tests/e2e.py to validate the proxy without MySQL.

Speaks MCP stdio JSON-RPC just enough to exercise the proxy code paths:
- initialize → returns fake serverInfo
- notifications/initialized → silent
- tools/list → returns a fake mysql_query tool
- tools/call → SELECT/SHOW/etc → success payload, DROP/TRUNCATE → error

Also writes each call to a heartbeat log so tests can count cold-starts.
"""

from __future__ import annotations

import json
import os
import sys
import time

HB = os.environ.get("FAKE_BACKEND_HB")


def log_call(method: str) -> None:
    if not HB:
        return
    with open(HB, "a", encoding="utf-8") as f:
        f.write(f"{time.time():.6f} {method}\n")


def reply(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def err(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def main() -> int:
    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method = msg.get("method")
        req_id = msg.get("id")
        if req_id is None:
            # notification, no reply
            log_call(f"notify:{method}")
            continue

        if method == "initialize":
            log_call("initialize")
            sys.stdout.write(json.dumps(reply(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mysql-router-mcp", "version": "0.0.1"},
            })) + "\n")
            sys.stdout.flush()
        elif method == "tools/list":
            log_call("tools/list")
            sys.stdout.write(json.dumps(reply(req_id, {
                "tools": [{
                    "name": "mysql_query",
                    "description": "fake",
                    "inputSchema": {"type": "object"},
                }],
            })) + "\n")
            sys.stdout.flush()
        elif method == "tools/call":
            args = msg.get("params", {}).get("arguments", {}) or {}
            sql = args.get("sql", "")
            log_call("tools/call:" + sql[:30])
            up = sql.strip().upper()
            if up.startswith("DROP") or up.startswith("TRUNCATE"):
                keyword = up.split()[0]
                sys.stdout.write(json.dumps(err(req_id, -32000,
                    f"DDL/DCL statement not allowed (keyword='{keyword}')")) + "\n")
                sys.stdout.flush()
            else:
                sys.stdout.write(json.dumps(reply(req_id, {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "database": args.get("database"),
                            "row_count": 1,
                            "rows": [{"ok": 1}],
                            "elapsed_ms": 1,
                        }),
                    }],
                    "isError": False,
                })) + "\n")
                sys.stdout.flush()
        else:
            sys.stdout.write(json.dumps(err(req_id, -32601, "method not found")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())