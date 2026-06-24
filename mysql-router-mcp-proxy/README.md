# mysql-router-mcp-proxy

`mysql-router-mcp` 的 **lazy-load 包装层**。

> 设计文档：[`docs/superpowers/specs/2026-06-24-mysql-router-mcp-design.md`](../docs/superpowers/specs/2026-06-24-mysql-router-mcp-design.md) 增量 v2

## 它做了什么

Claude Code / Codex 启动时只挂这个轻量 stdio proxy。**它不连 MySQL、不建连接池**。模型第一次调用 `mcp__mysql-router__mysql_query(...)` 时，proxy 才 spawn 后端 `mysql-router-mcp` 子进程，跑完 initialize 握手，转发请求。

后端空闲 ≥ `MYSQL_ROUTER_IDLE_TIMEOUT`（默认 60s）后被 reaper kill，下次用再 cold-start。

```
Claude Code ──stdio──► mysql-router-proxy  ← 永远在线, 不连库
                            │
                            │ 第一次 call_tool
                            ▼
                       mysql-router-mcp     ← 真连 MySQL, 空闲 60s 后自杀
```

## 安装

```bash
cd "D:/claude/mysql-router-mcp-proxy"
uv sync
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `BACKEND_DIR` | `D:\claude\mysql-router-mcp` | 后端工作目录 |
| `BACKEND_CMD` | `uv` | 启动后端的命令 |
| `BACKEND_ARGS` | `run mysql-router-mcp` | 启动后端的参数（空格分隔） |
| `MYSQL_ROUTER_IDLE_TIMEOUT` | `60` | 空闲 N 秒后 reaper 杀后端 |
| `MYSQL_ROUTER_STARTUP_TIMEOUT` | `30` | cold-start + initialize 超时 |
| `MYSQL_ROUTER_EXEC_TIMEOUT` | `60` | 单次 tools/call 超时 |
| `MYSQL_ROUTER_PROXY_LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING |

后端所需的 `MYSQL_HOST` / `MYSQL_PASS` / `MYSQL_DATABASES` 等仍由后端自己读，不归 proxy 管。

## 接入 Claude Code

`~/.claude.json`（删掉所有 `mysql-xxxgroup` 之后）：

```jsonc
"mysql-router": {
  "type": "stdio",
  "command": "uv",
  "args": [
    "--directory", "D:\\claude\\mysql-router-mcp-proxy",
    "run", "mysql-router-proxy"
  ],
  "env": {
    "BACKEND_DIR": "D:\\claude\\mysql-router-mcp"
  }
}
```

`.codex/config.toml`（TOML 格式）：

```toml
[mcp_servers.mysql-router]
type = "stdio"
command = "uv"
args = ["--directory", "D:\\claude\\mysql-router-mcp-proxy", "run", "mysql-router-proxy"]

[mcp_servers.mysql-router.env]
BACKEND_DIR = "D:\\claude\\mysql-router-mcp"
MYSQL_ROUTER_IDLE_TIMEOUT = "60"
```

## 验证清单

1. 启动 proxy 后立即 `ps` / 任务管理器看 Python 进程数 —— 应只有 1 个（proxy 本身）
2. 调用一次 `mcp__mysql-router__mysql_query(...)` —— Python 进程数变 2（proxy + backend）
3. 等 60s 不调用 —— backend 应被 kill，Python 进程数回到 1
4. 再调用一次 —— 进程数变 2（重新 cold-start）

## 与 backend 的差异

| 维度 | backend (mysql-router-mcp) | proxy (mysql-router-mcp-proxy) |
|---|---|---|
| 依赖 | mcp + aiomysql | 仅 mcp |
| 启动开销 | 建 aiomysql 连接池 | 零 |
| 空闲行为 | 常驻 | 60s 后自杀 backend |
| 调用者 | 永远不该直接挂 Claude Code | 应该挂 Claude Code |
| 工具数 | 1 (mysql_query) | 1 (mysql_query，静态) |