# agent-mcp

A monorepo of MCP (Model Context Protocol) servers for AI agents — currently
focused on safe, single-process MySQL access for Claude Code and other MCP clients.

> 📖 **Start here:** see [OVERVIEW.md](./OVERVIEW.md) for the full architecture,
> installation, and security model.

## What's in here

| Path | What it is |
|---|---|
| [`OVERVIEW.md`](./OVERVIEW.md) | Project introduction, architecture, usage, security model, bug-fix history |
| [`docs/design.md`](./docs/design.md) | Original 2026-06-24 design spec (rationale, alternatives considered) |
| [`mysql-router-mcp/`](./mysql-router-mcp/) | The backend: a single MCP server that owns an aiomysql pool and exposes one `mysql_query` tool with a `database` argument |
| [`mysql-router-mcp-proxy/`](./mysql-router-mcp-proxy/) | The lazy-load stdio wrapper that fronts the backend — Claude Code only ever talks to this |

## Quick start

```bash
# 1. Install both packages
cd mysql-router-mcp     && uv sync && cd ..
cd mysql-router-mcp-proxy && uv sync && cd ..

# 2. Set env vars (see OVERVIEW.md for full list)
export MYSQL_HOST=127.0.0.1
export MYSQL_USER=root
export MYSQL_PASS=MySQLpassword        # ← never commit real passwords
export MYSQL_DATABASES=usergroup,papergroup,...
export MYSQL_WRITABLE_DATABASES=usergroup,papergroup,...
export MYSQL_ALLOW_DDL=true             # if you want CREATE/DROP TABLE

# 3. Wire into Claude Code (paste into ~/.claude.json → mcpServers)
# See OVERVIEW.md § "Wire into Claude Code"

# 4. Open a new terminal (env vars only propagate to new processes) and run:
claude
```

Then in a Claude session:

```
mcp__mysql-router__mysql_query(
  database="usergroup",
  sql="SELECT DATABASE() AS db, NOW() AS now"
)
```

## Why a monorepo

The backend (`mysql-router-mcp`) and its proxy (`mysql-router-mcp-proxy`) are
two halves of one MCP product — you can't use one without the other, they
share a single design document, and they ship together. Keeping them in
separate repos would force every PR to touch two READMEs and a release tag.

## License

MIT — see [`LICENSE`](./LICENSE).