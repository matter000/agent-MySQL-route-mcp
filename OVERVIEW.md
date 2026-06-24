# Agent MCP — MySQL Router for Claude Code

A unified MySQL access layer for Claude Code (and any MCP-compatible client) that
replaces the usual `one stdio process per database` mess with **one connection pool**
fronted by a single `mysql_query` tool that takes `database` as an argument.

> **TL;DR** — instead of 13 separate `mcp__mysql-usergroup__mysql_query`,
> `mcp__mysql-papergroup__mysql_query`, … processes, you install one
> `mysql-router` MCP and call `mcp__mysql-router__mysql_query(database="usergroup", sql=...)`.

---

## Why this exists

If you give Claude Code access to multiple MySQL databases, the naïve setup is:

```
mcpServers:
  mysql-usergroup:   { npx @benborla29/mcp-server-mysql, env: { MYSQL_DB: usergroup,   MYSQL_PASS: ... } }
  mysql-papergroup:  { npx @benborla29/mcp-server-mysql, env: { MYSQL_DB: papergroup,  MYSQL_PASS: ... } }
  mysql-newsgroup:   { npx @benborla29/mcp-server-mysql, env: { MYSQL_DB: newsgroup,   MYSQL_PASS: ... } }
  ... (×13)
```

Problems:

1. **13 stdio processes.** Every Claude Code startup pays the cost of spawning,
   importing, and initializing 13 Node.js MCP servers, each opening a TCP
   connection to MySQL.
2. **Tool namespace pollution.** 13 `mcp__mysql-xxxgroup__mysql_query` tools
   crowd the model's tool list with near-duplicates.
3. **Same password written 13 times.** Violates the `no plaintext secrets in
   config files` principle — if you accidentally commit one of those blocks,
   you leaked the DB password.
4. **DDL allowed by default.** Each `benborla29/mcp-server-mysql` instance
   permits `DROP`/`TRUNCATE`/`ALTER` with no toggle, so a typo can wipe a
   production table.
5. **No defense against SQL injection via `;` chaining.** Multi-statement
   queries (`SELECT 1; DROP TABLE users`) are accepted as-is by default.

This repo fixes all five.

---

## Architecture

```
Claude Code  ──stdio──►  mysql-router-proxy            (always online, ~50ms startup)
                              │
                              │ first call_tool → spawns child:
                              ▼
                         mysql-router-mcp             (aiomysql pool, 13 schemas)
                              │
                              ▼
                         MySQL 127.0.0.1:3306
```

| Component | Role | Cold-start cost |
|---|---|---|
| `mysql-router-mcp-proxy` | Thin stdio proxy. Holds the Claude Code stdio socket, lazy-spawns the backend on first `tools/call`, kills it after `MYSQL_ROUTER_IDLE_TIMEOUT` seconds of inactivity. | **~50ms** (just imports `mcp[cli]`) |
| `mysql-router-mcp` | The real worker. Owns the aiomysql connection pool, validates every SQL statement, classifies it as read/write/DDL, and routes to the correct schema. | ~500ms (imports `aiomysql`, creates pool) |
| MySQL | Your existing instance. | — |

After 60 seconds of no traffic, the backend is reaped and the proxy stays
resident. The next call cold-starts the backend again (~500ms) — usually
imperceptible for the model.

---

## Repository layout

```
agent-mcp/
├── OVERVIEW.md                      ← you are here
├── README.md                        ← top-level pointer
├── docs/
│   └── design.md                    ← original design spec (2026-06-24)
├── mysql-router-mcp/                ← the worker
│   ├── src/mysql_router_mcp/
│   │   ├── __init__.py
│   │   └── server.py
│   ├── tests/
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── .env.example
│   ├── .gitignore
│   └── README.md
└── mysql-router-mcp-proxy/          ← the stdio lazy-load wrapper
    ├── src/mysql_router_proxy/
    │   ├── __init__.py
    │   └── proxy.py
    ├── tests/
    ├── pyproject.toml
    ├── uv.lock
    └── README.md
```

---

## Tool API

A single tool is exposed: `mysql_query`.

### Arguments

| Name | Type | Required | Description |
|---|---|---|---|
| `database` | string | ✓ | Target schema. Must be in `MYSQL_DATABASES`. The model cannot accidentally hit the wrong DB — it's a required tool argument, not a config option. |
| `sql` | string | ✓ | A **single** SQL statement. Multi-statement SQL is rejected. |
| `params` | array | | Bound parameters for `%s` placeholders. Use this for any user-supplied value to prevent injection. |

### Example calls

```python
# Read
mcp__mysql-router__mysql_query(
    database="usergroup",
    sql="SELECT id, name FROM users WHERE status = %s LIMIT 10",
    params=["active"]
)

# Write (requires database in MYSQL_WRITABLE_DATABASES)
mcp__mysql-router__mysql_query(
    database="papergroup",
    sql="UPDATE papers SET status='archived' WHERE last_viewed < %s",
    params=["2025-01-01"]
)

# DDL (requires MYSQL_ALLOW_DDL=true)
mcp__mysql-router__mysql_query(
    database="usergroup",
    sql="CREATE TABLE feature_flags (name VARCHAR(64) PRIMARY KEY, enabled BOOLEAN)"
)
```

### Return shape

For reads:

```json
{
  "database": "usergroup",
  "category": "read",
  "row_count": 10,
  "truncated": false,
  "rows": [["...", "..."], ...],
  "elapsed_ms": 4
}
```

For writes / DDL:

```json
{
  "database": "usergroup",
  "category": "write",
  "affected_rows": 42,
  "rows": [],
  "elapsed_ms": 12
}
```

---

## Security model

The defense-in-depth layers, in order from outermost to innermost:

1. **Database whitelist.** `database` must appear in `MYSQL_DATABASES`. Anything
   else is rejected before any SQL is sent to MySQL. This is enforced by
   `server.py:270`.
2. **Read-only by default.** `MYSQL_DEFAULT_READ_ONLY=true` is the default.
   Writes are rejected unless `database` is in `MYSQL_WRITABLE_DATABASES`.
3. **DDL blacklisted by default.** `DROP`, `TRUNCATE`, `ALTER`, `GRANT`,
   `REVOKE`, `RENAME`, `CREATE USER/ROLE/INDEX/FUNCTION/PROCEDURE/TRIGGER/
   DATABASE/SCHEMA/TABLE/TEMPORARY TABLE`, `REPLACE INTO` are rejected unless
   `MYSQL_ALLOW_DDL=true`.
4. **Multi-statement rejected.** Any `;` followed by non-whitespace content
   (after stripping comments) returns `multi-statement SQL is not allowed`.
   This stops `SELECT 1; DROP TABLE users` from sneaking through.
5. **Parameterized queries supported.** Use `params=[...]` for any value that
   came from a user / the model.

These four checks happen **before** the SQL reaches MySQL. Even if MySQL
permissions are wide open, the MCP itself refuses to send dangerous
statements.

---

## Configuration reference

All configuration is read from environment variables. **Never** write
real credentials into `~/.claude.json` or any committed config file — read
them from the shell environment instead.

| Variable | Required | Default | Description |
|---|---|---|---|
| `MYSQL_HOST` | ✓ | `127.0.0.1` | MySQL host |
| `MYSQL_PORT` | | `3306` | MySQL port |
| `MYSQL_USER` | ✓ | — | MySQL user |
| `MYSQL_PASS` | ✓ | — | MySQL password (read from env, **never committed**) |
| `MYSQL_DATABASES` | ✓ | — | Comma-separated whitelist of schemas this MCP may touch |
| `MYSQL_WRITABLE_DATABASES` | | empty | Subset of `MYSQL_DATABASES` allowed to receive writes |
| `MYSQL_DEFAULT_READ_ONLY` | | `true` | If `true`, writes outside `MYSQL_WRITABLE_DATABASES` are rejected |
| `MYSQL_ALLOW_DDL` | | `false` | If `false`, DDL statements are rejected |
| `MYSQL_MAX_ROWS` | | `1000` | Cap on rows returned by a single read |
| `MYSQL_MAX_EXEC_SECONDS` | | `30` | Per-query timeout |
| `MYSQL_POOL_MIN` / `MYSQL_POOL_MAX` | | `1` / `5` | aiomysql connection pool size |
| `BACKEND_DIR` | | `D:\claude\mysql-router-mcp` | (proxy only) directory of backend |
| `MYSQL_ROUTER_IDLE_TIMEOUT` | | `60` | (proxy only) seconds of inactivity before backend is killed |

---

## Installation

### 1. Install both packages

```bash
cd mysql-router-mcp
uv sync

cd ../mysql-router-mcp-proxy
uv sync
```

### 2. Set environment variables

PowerShell (User-level, persistent):

```powershell
[System.Environment]::SetEnvironmentVariable("MYSQL_HOST", "127.0.0.1", "User")
[System.Environment]::SetEnvironmentVariable("MYSQL_USER", "root", "User")
[System.Environment]::SetEnvironmentVariable("MYSQL_PASS", "MySQLpassword", "User")
[System.Environment]::SetEnvironmentVariable(
  "MYSQL_DATABASES",
  "usergroup,papergroup,newsgroup,comments_and_ratings,feedbackgroup,administratorgroup,aigroup,analyticsgroup,institutiongroup,messagegroup,noticegroup,notificationgroup,qagroup",
  "User"
)
[System.Environment]::SetEnvironmentVariable("MYSQL_WRITABLE_DATABASES", "usergroup,papergroup,newsgroup,comments_and_ratings,feedbackgroup,administratorgroup,aigroup,analyticsgroup,institutiongroup,messagegroup,noticegroup,notificationgroup,qagroup", "User")
[System.Environment]::SetEnvironmentVariable("MYSQL_ALLOW_DDL", "true", "User")
```

Git Bash:

```bash
echo 'export MYSQL_HOST=127.0.0.1' >> ~/.bashrc
echo 'export MYSQL_USER=root' >> ~/.bashrc
echo 'export MYSQL_PASS=MySQLpassword' >> ~/.bashrc
echo 'export MYSQL_DATABASES=usergroup,papergroup,newsgroup,comments_and_ratings,feedbackgroup,administratorgroup,aigroup,analyticsgroup,institutiongroup,messagegroup,noticegroup,notificationgroup,qagroup' >> ~/.bashrc
echo 'export MYSQL_WRITABLE_DATABASES=usergroup,papergroup,newsgroup,comments_and_ratings,feedbackgroup,administratorgroup,aigroup,analyticsgroup,institutiongroup,messagegroup,noticegroup,notificationgroup,qagroup' >> ~/.bashrc
echo 'export MYSQL_ALLOW_DDL=true' >> ~/.bashrc
source ~/.bashrc
```

> ⚠️ On Windows, env vars set at user level only propagate to **newly launched**
> processes. After setting them, you must **open a new PowerShell / Git Bash
> window** before launching Claude Code. Re-launching in an existing terminal
> will silently use the old (empty) environment and the MCP will fail to start.

### 3. Wire into Claude Code

Add to your `~/.claude.json` (global mcpServers section):

```jsonc
"mcpServers": {
  "mysql-router": {
    "type": "stdio",
    "command": "uv",
    "args": [
      "--directory", "D:\\claude\\mysql-router-mcp-proxy",
      "run", "mysql-router-proxy"
    ],
    "env": {
      "BACKEND_DIR": "D:\\claude\\mysql-router-mcp",
      "MYSQL_ROUTER_IDLE_TIMEOUT": "60"
    }
  }
}
```

The connection settings come from the environment; `env` here is only for
proxy-level config (backend location, idle timeout).

### 4. Restart Claude Code

```bash
# In a fresh terminal that has the env vars loaded:
claude
```

In the new session, run a smoke test:

```
mcp__mysql-router__mysql_query(
  database="usergroup",
  sql="SELECT DATABASE() AS db, NOW() AS now"
)
```

Expected: `{"database": "usergroup", "category": "read", "row_count": 1, "rows": [["usergroup", "..."]]}`

---

## Bug fix history

This section is auto-generated and tracks the bug fixes applied to
`mysql-router-mcp/server.py`.

### 2026-06-24 — 4 fixes shipped together

| # | File:line | Bug | Fix |
|---|---|---|---|
| 1 | `server.py:284` | `if category == "write": raise ValueError(err)` — writes were **unconditionally** rejected regardless of writable DB list, making the entire write path dead. | Changed to `if category == "write" and err:` so the raise only fires when the writable-DB check actually failed. |
| 2 | `server.py:290` | After fix #1, writes started returning `1046 No database selected` because the shared aiomysql pool never had a default DB bound. | Added `await conn.select_db(database)` per call before `cur.execute`. |
| 3 | `server.py:55` | `_DDL_PATTERN` matched `CREATE USER/ROLE/INDEX/FUNCTION/PROCEDURE/TRIGGER/DATABASE/SCHEMA` but **not** `CREATE TABLE` or `CREATE TEMPORARY TABLE`. | Added `TABLE\|TEMPORARY\s+TABLE` to the alternation. |
| 4 | `server.py:201` | Read keyword set did not include `WITH`, so CTEs (`WITH cte AS (...) SELECT ...`) were classified as writes. | Added `"WITH"` to the set. |

Verified end-to-end after fixes: 13 databases × (SELECT + CREATE TEMP + INSERT + SELECT-back + DROP TEMP) = 65/65 calls PASS. See `mysql-router-mcp/README.md` for the verification protocol.

---

## License

MIT (see `LICENSE`).