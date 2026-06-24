"""End-to-end test: spawn the proxy, drive it via stdio JSON-RPC, verify lazy-load behaviour.

What we verify:
1. Proxy registers mysql_query without spawning backend (zero DB cost at startup).
2. First tools/call cold-starts backend, hands back real response.
3. Second tools/call hits warm backend (no extra spawn).
4. DDL is rejected (backend's safety policy still in effect through proxy).
5. Idle timeout kills backend; next call cold-starts again.

We DON'T talk to MySQL for real — we point BACKEND_ARGS at tests/fake_backend.py.
That keeps the test deterministic, offline, and fast.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
FAKE_BACKEND = REPO / "tests" / "fake_backend.py"


async def _read_message(stdout: asyncio.StreamReader) -> dict[str, Any]:
    line = await stdout.readline()
    assert line, "server closed stdout"
    return json.loads(line.decode("utf-8"))


async def _send(stdin: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
    await stdin.drain()


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.open(encoding="utf-8"))


async def main() -> int:
    hb = REPO / "tests" / "_hb.log"
    if hb.exists():
        hb.unlink()

    env = os.environ.copy()
    env["BACKEND_DIR"] = str(REPO)
    env["BACKEND_CMD"] = sys.executable
    env["BACKEND_ARGS"] = str(FAKE_BACKEND)
    env["FAKE_BACKEND_HB"] = str(hb)
    env["MYSQL_ROUTER_IDLE_TIMEOUT"] = "2"
    env["MYSQL_ROUTER_STARTUP_TIMEOUT"] = "10"
    env["MYSQL_ROUTER_EXEC_TIMEOUT"] = "10"
    env["MYSQL_ROUTER_PROXY_LOG_LEVEL"] = "WARNING"

    # Spawn proxy directly via Python to avoid uv's 3-5s startup cost.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "import mysql_router_proxy.proxy; mysql_router_proxy.proxy.cli()",
        cwd=str(REPO),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout

    results: list[tuple[str, bool, str]] = []
    try:
        # 1. initialize handshake
        await _send(proc.stdin, {
            "jsonrpc": "2.0", "id": "1", "method": "initialize",
            "params": {"protocolVersion": "2024-11-05",
                       "capabilities": {},
                       "clientInfo": {"name": "test-client", "version": "0"}},
        })
        resp = await _read_message(proc.stdout)
        ok = resp.get("result", {}).get("serverInfo", {}).get("name") == "mysql-router-proxy"
        results.append(("initialize returns proxy serverInfo", ok, f"got: {resp}"))

        await _send(proc.stdin, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 2. tools/list should NOT spawn backend
        await _send(proc.stdin, {"jsonrpc": "2.0", "id": "2", "method": "tools/list"})
        resp = await _read_message(proc.stdout)
        tools = resp.get("result", {}).get("tools", [])
        results.append(("tools/list returns mysql_query",
                        any(t.get("name") == "mysql_query" for t in tools),
                        f"got: {tools}"))
        results.append(("tools/list did NOT spawn backend",
                        _count_lines(hb) == 0,
                        f"backend hb lines: {_count_lines(hb)}"))

        # 3. first tools/call — cold-start backend
        await _send(proc.stdin, {
            "jsonrpc": "2.0", "id": "3", "method": "tools/call",
            "params": {"name": "mysql_query",
                       "arguments": {"database": "usergroup", "sql": "SELECT 1"}},
        })
        resp = await _read_message(proc.stdout)
        content = resp.get("result", {}).get("content", [])
        ok = bool(content) and '"row_count": 1' in content[0]["text"]
        results.append(("first tools/call succeeds (cold-start path)", ok, f"got: {content}"))
        cold_calls = _count_lines(hb)
        results.append(("first tools/call spawned backend (hb lines >= 2: init+call)",
                        cold_calls >= 2, f"hb lines: {cold_calls}"))

        # 4. second tools/call — warm backend
        before = _count_lines(hb)
        await _send(proc.stdin, {
            "jsonrpc": "2.0", "id": "4", "method": "tools/call",
            "params": {"name": "mysql_query",
                       "arguments": {"database": "usergroup", "sql": "SELECT 2"}},
        })
        resp = await _read_message(proc.stdout)
        content = resp.get("result", {}).get("content", [])
        ok = bool(content) and '"row_count": 1' in content[0]["text"]
        results.append(("second tools/call succeeds (warm path)", ok, f"got: {content}"))
        after = _count_lines(hb)
        # warm path adds exactly 1 line (the call itself), not 2 (no new initialize)
        results.append(("warm path did NOT re-spawn (added exactly 1 hb line)",
                        after == before + 1, f"before={before} after={after}"))

        # 5. DDL rejected — MCP wraps handler exceptions as result.isError=True
        await _send(proc.stdin, {
            "jsonrpc": "2.0", "id": "5", "method": "tools/call",
            "params": {"name": "mysql_query",
                       "arguments": {"database": "usergroup", "sql": "DROP TABLE x"}},
        })
        resp = await _read_message(proc.stdout)
        is_err = resp.get("result", {}).get("isError") is True
        results.append(("DDL is rejected through proxy (result.isError=True)",
                        is_err, f"got: {resp}"))

        # 6. wait > idle_timeout
        print("[test] waiting 4s for reaper to kill backend...", file=sys.stderr)
        await asyncio.sleep(4)

        # 7. next call re-cold-starts
        await _send(proc.stdin, {
            "jsonrpc": "2.0", "id": "7", "method": "tools/call",
            "params": {"name": "mysql_query",
                       "arguments": {"database": "usergroup", "sql": "SELECT 3"}},
        })
        resp = await _read_message(proc.stdout)
        content = resp.get("result", {}).get("content", [])
        ok = bool(content) and '"row_count": 1' in content[0]["text"]
        results.append(("after idle timeout, next call re-cold-starts", ok, f"got: {content}"))
        results.append(("re-cold-start ran initialize handshake again",
                        _count_lines(hb) >= 5,  # 2 (cold1) + 1 (warm) + 1 (DDL) + 2 (cold2)
                        f"hb lines: {_count_lines(hb)}"))

    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        stderr = (await proc.stderr.read()).decode("utf-8", errors="replace")  # type: ignore[union-attr]

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} tests passed ===\n")
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}")
        if not ok:
            print(f"        detail: {detail}")
    if passed != len(results):
        print("\n--- proxy stderr (last 2000 chars) ---")
        print(stderr[-2000:])
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))