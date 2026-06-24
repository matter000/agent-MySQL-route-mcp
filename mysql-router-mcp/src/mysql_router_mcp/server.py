"""MCP server that exposes a single ``mysql_query`` tool routing to one of N MySQL databases.

Design goals (see docs/superpowers/specs/2026-06-24-mysql-router-mcp-design.md):

* One MCP server instead of N — uses a shared aiomysql pool.
* ``database`` is a required tool arg → caller cannot accidentally hit the wrong DB.
* Password is read from env, never written to MCP config (CLAUDE.md red-line #1).
* Default read-only; writes only allowed for databases in ``MYSQL_WRITABLE_DATABASES``.
* DDL statements are rejected unless ``MYSQL_ALLOW_DDL=true``.
* Multi-statement queries are rejected (defense against injection via ``;`` chaining).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

import aiomysql
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ---------------------------------------------------------------------------
# Logging — go to stderr so it never pollutes the MCP stdio JSON-RPC stream.
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("MYSQL_ROUTER_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s mysql-router-mcp: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mysql-router-mcp")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Statements that are *always* dangerous, even inside a writable database.
_DDL_PATTERN = re.compile(
    r"^\s*(?:/\*.*?\*/\s*)?"          # optional leading /* ... */
    r"(?:--[^\n]*\n\s*)*"             # optional leading -- line comments
    r"(?:"                            # then one of the dangerous verbs:
    r"\bDROP\b"
    r"|\bTRUNCATE\b"
    r"|\bALTER\b"
    r"|\bGRANT\b"
    r"|\bREVOKE\b"
    r"|\bRENAME\b"
    r"|\bCREATE\s+(?:USER|ROLE|INDEX|FUNCTION|PROCEDURE|TRIGGER|DATABASE|SCHEMA|TABLE|TEMPORARY\s+TABLE)\b"
    r"|\bREPLACE\s+INTO\b"            # REPLACE = DELETE+INSERT, treat as write
    r")",
    re.IGNORECASE | re.DOTALL,
)

# First non-whitespace, non-comment word of the SQL.
_FIRST_TOKEN_RE = re.compile(
    r"^\s*(?:/\*.*?\*/\s*)?(?:--[^\n]*\n\s*)*(\w+)",
    re.DOTALL,
)


def _csv_env(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default).strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _required_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(
            f"missing required env var: {name}. "
            f"Set it in your shell / .env / Claude Code env block."
        )
    return val


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"env var {name} must be an integer, got {raw!r}") from exc


def load_config() -> dict[str, Any]:
    """Load + validate configuration from environment."""
    databases = _csv_env("MYSQL_DATABASES")
    if not databases:
        raise RuntimeError(
            "MYSQL_DATABASES must list at least one schema (comma-separated)."
        )
    writable = set(_csv_env("MYSQL_WRITABLE_DATABASES"))
    unknown_writable = writable - set(databases)
    if unknown_writable:
        raise RuntimeError(
            f"MYSQL_WRITABLE_DATABASES contains unknown DBs: {sorted(unknown_writable)}. "
            f"All writable DBs must also appear in MYSQL_DATABASES."
        )
    return {
        "host": os.environ.get("MYSQL_HOST", "127.0.0.1").strip(),
        "port": _int_env("MYSQL_PORT", 3306),
        "user": _required_env("MYSQL_USER"),
        "password": os.environ.get("MYSQL_PASS", ""),  # may be empty for unix-socket / trust auth
        "databases": databases,
        "writable_databases": writable,
        "default_read_only": _bool_env("MYSQL_DEFAULT_READ_ONLY", True),
        "allow_ddl": _bool_env("MYSQL_ALLOW_DDL", False),
        "max_rows": _int_env("MYSQL_MAX_ROWS", 1000),
        "max_exec_seconds": _int_env("MYSQL_MAX_EXEC_SECONDS", 30),
        "pool_min": _int_env("MYSQL_POOL_MIN", 1),
        "pool_max": _int_env("MYSQL_POOL_MAX", 5),
    }


# ---------------------------------------------------------------------------
# SQL safety
# ---------------------------------------------------------------------------


def _strip_sql(sql: str) -> str:
    """Strip comments + leading whitespace; reject if anything follows the first ``;``."""
    s = sql.strip()
    # Remove /* ... */ block comments (non-greedy, dotall).
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.DOTALL)
    # Remove -- line comments.
    s = re.sub(r"--[^\n]*", " ", s)
    # Remove # line comments (MySQL extension).
    s = re.sub(r"(?m)^\s*#.*$", " ", s)
    return s.strip()


def _first_keyword(sql: str) -> str:
    m = _FIRST_TOKEN_RE.match(sql)
    return m.group(1).upper() if m else ""


def _is_multi_statement(sql: str) -> bool:
    """Detect any ``;`` followed by non-whitespace / non-comment chars."""
    s = _strip_sql(sql)
    if not s.endswith(";"):
        return True  # bare SQL without trailing ; counts as one statement only if no inner ;
    # Strip the trailing ;.
    body = s[:-1]
    # Anything left ending with content + ;  → multi-statement.
    return bool(body.rstrip().endswith(";"))


def classify_sql(sql: str, cfg: dict[str, Any], database: str) -> tuple[str, str]:
    """Return (category, error_message). category ∈ {"read", "write", "ddl", "multi"}.

    Raises nothing — errors are returned as category="error" with a message.
    """
    stripped = _strip_sql(sql)
    if not stripped:
        return ("error", "SQL is empty after stripping comments.")

    # Multi-statement guard (must come first so ";DROP TABLE x" doesn't sneak through).
    # Allow a single trailing ; but reject any inner ; followed by content.
    s = stripped.rstrip(";").rstrip()
    if ";" in s:
        return ("multi", "multi-statement SQL is not allowed; send one statement per call.")

    keyword = _first_keyword(stripped)
    if not keyword:
        return ("error", "SQL must start with a recognizable keyword.")

    # DDL blacklist — checked before write/read because DDL is dangerous even on writable DBs.
    if _DDL_PATTERN.match(stripped):
        if not cfg["allow_ddl"]:
            return (
                "ddl",
                f"DDL/DCL statement not allowed (keyword={keyword!r}). "
                f"Set MYSQL_ALLOW_DDL=true to override (not recommended).",
            )
        # Even when DDL is allowed, the database must be in writable list.
        if cfg["default_read_only"] and database not in cfg["writable_databases"]:
            return (
                "ddl",
                f"DDL on {database!r} denied: database is read-only.",
            )
        return ("ddl", "")

    # SELECT / SHOW / DESCRIBE / EXPLAIN are always reads.
    if keyword in {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "USE", "WITH"}:
        return ("read", "")

    # Everything else is a write.
    if cfg["default_read_only"] and database not in cfg["writable_databases"]:
        return (
            "write",
            f"writes on {database!r} denied: default read-only mode is on and "
            f"{database!r} is not in MYSQL_WRITABLE_DATABASES.",
        )
    return ("write", "")


# ---------------------------------------------------------------------------
# Pool management
# ---------------------------------------------------------------------------


class PoolHolder:
    """Lazily-initialised aiomysql pool + per-request connection acquisition."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self._pool: aiomysql.Pool | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._pool is not None:
                return
            log.info(
                "creating aiomysql pool host=%s port=%s user=%s min=%s max=%s",
                self.cfg["host"], self.cfg["port"], self.cfg["user"],
                self.cfg["pool_min"], self.cfg["pool_max"],
            )
            self._pool = await aiomysql.create_pool(
                host=self.cfg["host"],
                port=self.cfg["port"],
                user=self.cfg["user"],
                password=self.cfg["password"],
                minsize=self.cfg["pool_min"],
                maxsize=self.cfg["pool_max"],
                autocommit=True,
                charset="utf8mb4",
            )

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                self._pool.close()
                await self._pool.wait_closed()
                self._pool = None

    @asynccontextmanager
    async def connection(self):
        if self._pool is None:
            await self.start()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            yield conn


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


async def mysql_query_impl(pool: PoolHolder, cfg: dict[str, Any], database: str, sql: str, params: list | None) -> dict[str, Any]:
    database = database.strip()
    if database not in cfg["databases"]:
        raise ValueError(
            f"database {database!r} is not in MYSQL_DATABASES whitelist. "
            f"Allowed: {cfg['databases']}"
        )

    category, err = classify_sql(sql, cfg, database)
    if category == "error":
        raise ValueError(err)
    if category == "multi":
        raise ValueError(err)
    if category == "ddl" and not cfg["allow_ddl"]:
        raise ValueError(err)
    if category == "write" and err:
        raise ValueError(err)

    started = time.monotonic()
    async with pool.connection() as conn:
        # Shared pool has no per-connection default db; bind it per-call so
        # DML / DDL / TEMP TABLE all resolve to the target schema.
        await conn.select_db(database)
        async with conn.cursor() as cur:
            try:
                await asyncio.wait_for(
                    cur.execute(sql, params or None),
                    timeout=cfg["max_exec_seconds"],
                )
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"SQL exceeded MYSQL_MAX_EXEC_SECONDS={cfg['max_exec_seconds']}"
                ) from exc

            # For reads, fetch rows. For writes/DDL, report affected rows.
            if category == "read":
                rows = await cur.fetchmany(cfg["max_rows"] + 1)
                truncated = len(rows) > cfg["max_rows"]
                if truncated:
                    rows = rows[: cfg["max_rows"]]
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return {
                    "database": database,
                    "category": category,
                    "row_count": len(rows),
                    "truncated": truncated,
                    "rows": rows,
                    "elapsed_ms": elapsed_ms,
                }
            else:
                # DDL or write — no result set.
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return {
                    "database": database,
                    "category": category,
                    "affected_rows": cur.rowcount,
                    "rows": [],
                    "elapsed_ms": elapsed_ms,
                }


# ---------------------------------------------------------------------------
# MCP glue
# ---------------------------------------------------------------------------


TOOL_DEFINITION = Tool(
    name="mysql_query",
    description=(
        "Run a single SQL statement against one of the whitelisted MySQL databases. "
        "The `database` argument selects the target schema from MYSQL_DATABASES "
        "(one of: configurable, see tool description in server logs). "
        "Returns JSON with `rows`, `row_count`, and `elapsed_ms`. "
        "By default the server is read-only; writes require the database to be in "
        "MYSQL_WRITABLE_DATABASES. DDL (DROP/TRUNCATE/ALTER/...) is rejected unless "
        "MYSQL_ALLOW_DDL=true. Multi-statement SQL is rejected."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "database": {
                "type": "string",
                "description": "Target database name. Must be in MYSQL_DATABASES.",
            },
            "sql": {
                "type": "string",
                "description": "Single SQL statement to execute.",
            },
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


def build_server(cfg: dict[str, Any], pool: PoolHolder) -> Server:
    server = Server("mysql-router-mcp")

    @server.list_tools()
    async def list_tools():
        # Patch the database whitelist into the description on the fly so the model
        # can see what schemas it is allowed to call.
        allowed = ", ".join(cfg["databases"])
        patched = Tool(
            name=TOOL_DEFINITION.name,
            description=(
                TOOL_DEFINITION.description
                + f" Whitelisted databases: {allowed}."
                + (f" Writable databases: {sorted(cfg['writable_databases'])}." if cfg["writable_databases"] else " Default is read-only on all databases.")
            ),
            inputSchema=TOOL_DEFINITION.inputSchema,
        )
        return [patched]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]):
        if name != "mysql_query":
            raise ValueError(f"unknown tool: {name!r}")
        database = arguments.get("database")
        sql = arguments.get("sql")
        params = arguments.get("params") or []
        if not isinstance(database, str) or not database:
            raise ValueError("`database` must be a non-empty string")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("`sql` must be a non-empty string")
        if not isinstance(params, list):
            raise ValueError("`params` must be a list if provided")

        result = await mysql_query_impl(pool, cfg, database, sql, params)
        # Tool result content must be a list of content blocks.
        import json
        return [TextContent(type="text", text=json.dumps(result, default=str, ensure_ascii=False))]

    return server


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    cfg = load_config()
    log.info(
        "starting mysql-router-mcp; databases=%s writable=%s default_read_only=%s allow_ddl=%s",
        cfg["databases"], sorted(cfg["writable_databases"]),
        cfg["default_read_only"], cfg["allow_ddl"],
    )
    pool = PoolHolder(cfg)
    await pool.start()
    server = build_server(cfg, pool)

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        await pool.close()


def cli() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("mysql-router-mcp crashed")
        sys.exit(1)


if __name__ == "__main__":
    cli()