"""mysql-router-proxy: lazy-load stdio wrapper in front of mysql-router-mcp.

The backend (the real MCP server that talks to MySQL) is **not** spawned at proxy
startup. It is spawned on the first ``tools/call`` request, after which it stays
alive for at most ``MYSQL_ROUTER_IDLE_TIMEOUT`` seconds of inactivity. A background
reaper kills it after that, and the next call cold-starts it again.

Why: starting Claude Code / Codex with 19 MySQL MCPs spawned aiomysql connection
pools that were never used if the user never queried a DB. Proxy makes the
"register the tool" path free and pays for the "talk to MySQL" path only when
actually needed.

See docs/superpowers/specs/2026-06-24-mysql-router-mcp-design.md (incremental v2).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool

# ---------------------------------------------------------------------------
# Logging — stderr only (stdout is the MCP JSON-RPC channel).
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("MYSQL_ROUTER_PROXY_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s mysql-router-proxy: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mysql-router-proxy")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_cfg() -> dict[str, Any]:
    return {
        "backend_dir": os.environ.get("BACKEND_DIR", r"D:\claude\mysql-router-mcp").strip(),
        "backend_cmd": os.environ.get("BACKEND_CMD", "uv").strip(),
        "backend_args": os.environ.get("BACKEND_ARGS", "run mysql-router-mcp").split(),
        "idle_timeout": _int("MYSQL_ROUTER_IDLE_TIMEOUT", 60),
        "startup_timeout": _int("MYSQL_ROUTER_STARTUP_TIMEOUT", 30),
        "exec_timeout": _int("MYSQL_ROUTER_EXEC_TIMEOUT", 60),
    }


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"env {name} must be int, got {raw!r}") from exc


# ---------------------------------------------------------------------------
# Static tool definition — mirrors what the backend exposes, so the client
# sees a working list_tools() without spawning the backend.
# ---------------------------------------------------------------------------

STATIC_TOOL = Tool(
    name="mysql_query",
    description=(
        "Run a single SQL statement against one of the whitelisted MySQL databases. "
        "Pick the target schema via the `database` argument. "
        "By default the server is read-only; writes require the database to be in "
        "MYSQL_WRITABLE_DATABASES. DDL (DROP/TRUNCATE/ALTER/...) is rejected unless "
        "MYSQL_ALLOW_DDL=true. Multi-statement SQL is rejected."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "database": {
                "type": "string",
                "description": "Target database name. Must be in MYSQL_DATABASES whitelist.",
            },
            "sql": {"type": "string", "description": "Single SQL statement to execute."},
            "params": {
                "type": "array",
                "description": "Optional bound parameters for ?-style placeholders.",
                "items": {},
            },
        },
        "required": ["database", "sql"],
        "additionalProperties": False,
    },
)


# ---------------------------------------------------------------------------
# Backend subprocess manager
# ---------------------------------------------------------------------------


class Backend:
    """Owns one backend subprocess + its lifecycle (spawn, init, reap)."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.last_used: float = 0.0
        self.reaper: asyncio.Task[None] | None = None
        self.stderr_drain: asyncio.Task[None] | None = None

    def _touch(self) -> None:
        self.last_used = time.monotonic()

    async def ensure(self) -> asyncio.subprocess.Process:
        """Spawn (or reuse) backend + run initialize handshake once."""
        async with self.lock:
            if self.proc is not None and self.proc.returncode is None:
                self._touch()
                return self.proc

            log.info(
                "cold-starting backend: %s %s (cwd=%s)",
                self.cfg["backend_cmd"], self.cfg["backend_args"], self.cfg["backend_dir"],
            )
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    self.cfg["backend_cmd"],
                    *self.cfg["backend_args"],
                    cwd=self.cfg["backend_dir"],
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"failed to spawn backend {self.cfg['backend_cmd']!r}: {exc}. "
                    f"Check BACKEND_CMD / BACKEND_DIR env vars."
                ) from exc

            # Drain stderr in background; backend uses stderr for logs.
            self.stderr_drain = asyncio.create_task(self._drain_stderr())

            # Initialize handshake before forwarding any client request.
            await self._initialize_handshake()

            self._touch()
            if self.reaper is None or self.reaper.done():
                self.reaper = asyncio.create_task(self._reap_loop())
            return self.proc

    async def _send(self, msg: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        line = (json.dumps(msg) + "\n").encode("utf-8")
        self.proc.stdin.write(line)
        await self.proc.stdin.drain()

    async def _recv(self, timeout: float) -> dict[str, Any]:
        assert self.proc is not None and self.proc.stdout is not None
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=timeout)
        if not line:
            stderr_data = b""
            try:
                stderr_data = await asyncio.wait_for(self.proc.stderr.read(), timeout=0.5)  # type: ignore[union-attr]
            except Exception:
                pass
            raise RuntimeError(
                f"backend exited (rc={self.proc.returncode}); stderr: "
                f"{stderr_data.decode('utf-8', errors='replace')[:2000]}"
            )
        return json.loads(line.decode("utf-8"))

    async def _initialize_handshake(self) -> None:
        """Send initialize → wait for response → send notifications/initialized."""
        init_id = uuid.uuid4().hex
        await self._send({
            "jsonrpc": "2.0",
            "id": init_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mysql-router-proxy", "version": "0.1.0"},
            },
        })
        resp = await self._recv(self.cfg["startup_timeout"])
        if "error" in resp:
            raise RuntimeError(f"backend initialize failed: {resp['error']}")
        # Send notifications/initialized (no response expected).
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Forward a tools/call to the backend, return the raw response dict."""
        proc = await self.ensure()
        req_id = uuid.uuid4().hex
        await self._send({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        resp = await self._recv(self.cfg["exec_timeout"])
        self._touch()
        if resp.get("id") != req_id:
            # Should never happen but flag loudly if it does.
            log.warning("backend response id mismatch: sent=%s got=%s", req_id, resp.get("id"))
        return resp

    async def _drain_stderr(self) -> None:
        """Forward backend stderr lines to proxy stderr, prefixed."""
        try:
            while self.proc is not None and self.proc.returncode is None:
                assert self.proc.stderr is not None
                chunk = await self.proc.stderr.read(4096)
                if not chunk:
                    return
                sys.stderr.buffer.write(b"[backend] " + chunk)
                sys.stderr.buffer.flush()
        except Exception:
            return

    async def _reap_loop(self) -> None:
        """Wake every min(idle_timeout, 30)s; kill backend if idle past threshold."""
        try:
            while self.proc is not None and self.proc.returncode is None:
                await asyncio.sleep(min(self.cfg["idle_timeout"], 30))
                if self.proc is None or self.proc.returncode is not None:
                    return
                idle_for = time.monotonic() - self.last_used
                if idle_for < self.cfg["idle_timeout"]:
                    continue
                log.info("backend idle for %ds; terminating", int(idle_for))
                try:
                    self.proc.terminate()
                    try:
                        await asyncio.wait_for(self.proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        log.warning("backend did not exit on SIGTERM; killing")
                        self.proc.kill()
                        await self.proc.wait()
                except ProcessLookupError:
                    pass
                if self.stderr_drain is not None:
                    self.stderr_drain.cancel()
                    try:
                        await self.stderr_drain
                    except (asyncio.CancelledError, Exception):
                        pass
                self.proc = None
                return  # reaper exits; will be restarted on next request
        except Exception:
            log.exception("reaper crashed")


# ---------------------------------------------------------------------------
# MCP glue — proxy itself is a tiny MCP server with one static tool.
# ---------------------------------------------------------------------------


def build_server(cfg: dict[str, Any], backend: Backend) -> Server:
    server = Server("mysql-router-proxy")

    @server.list_tools()
    async def list_tools():
        # Static — does NOT spawn backend.
        return [STATIC_TOOL]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]):
        if name != "mysql_query":
            raise ValueError(f"unknown tool: {name!r}")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")

        resp = await backend.call_tool(name, arguments)
        # JSON-RPC level error (transport / parse / internal)
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"{err.get('code', 'error')}: {err.get('message', err)}")
        # Tool-level error (backend flagged the call itself as failed, e.g. DDL rejected)
        result = resp.get("result") or {}
        if result.get("isError"):
            content = result.get("content") or []
            text = content[0].get("text", "") if content else "backend error"
            raise RuntimeError(text or "backend error")
        content = result.get("content")
        if not isinstance(content, list):
            raise RuntimeError(f"backend returned malformed result: {result!r}")
        return content

    return server


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    cfg = load_cfg()
    log.info(
        "proxy starting; backend=%s %s in %s; idle_timeout=%ds",
        cfg["backend_cmd"], cfg["backend_args"], cfg["backend_dir"], cfg["idle_timeout"],
    )
    backend = Backend(cfg)
    server = build_server(cfg, backend)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def cli() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("proxy crashed")
        sys.exit(1)


if __name__ == "__main__":
    cli()