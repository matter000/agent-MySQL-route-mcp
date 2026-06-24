# mysql-router-mcp

一个 MCP server，用**单进程**取代你 `~/.claude.json` 里那一坨 `mysql-xxxgroup` 的 MySQL MCP。

> 设计文档：[`docs/superpowers/specs/2026-06-24-mysql-router-mcp-design.md`](../docs/superpowers/specs/2026-06-24-mysql-router-mcp-design.md)

## 为什么需要它

* 之前 13 个独立 stdio 进程（每个都 `npx @benborla29/mcp-server-mysql`），启动延迟高
* 工具命名空间被 13 个 `mcp__mysql-xxx__mysql_query` 污染
* 同一密码明文出现 13 次（违反项目 `CLAUDE.md` 红线）
* 所有库默认开了 DDL 权限，太危险

整合后只暴露一个工具 `mysql_query(database, sql, params?)`，把"选哪个库"放进工具签名，从源头杜绝选错库。

## 安装

```bash
cd "D:/claude/mysql-router-mcp"
uv sync           # 装依赖到 .venv
```

## 配置

**不要**把真实密码写进 `~/.claude.json`。设置系统环境变量（或 Claude Code 的 `env` 块）：

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `MYSQL_HOST` | ✓ | `127.0.0.1` | |
| `MYSQL_PORT` |   | `3306` | |
| `MYSQL_USER` | ✓ |   | |
| `MYSQL_PASS` | ✓ |   | 真实密码放这里，**不进 git** |
| `MYSQL_DATABASES` | ✓ |   | 逗号分隔：`usergroup,papergroup,newsgroup,...` |
| `MYSQL_DEFAULT_READ_ONLY` |   | `true` | 强烈建议保持 `true` |
| `MYSQL_WRITABLE_DATABASES` |   | 空 | 允许写操作的 db 子集 |
| `MYSQL_ALLOW_DDL` |   | `false` | 强烈建议保持 `false` |
| `MYSQL_MAX_ROWS` |   | `1000` | 单次返回行数上限 |
| `MYSQL_MAX_EXEC_SECONDS` |   | `30` | 单条 SQL 超时 |
| `MYSQL_POOL_MIN` / `MYSQL_POOL_MAX` |   | `1` / `5` | aiomysql 连接池 |

PowerShell 永久设置（管理员或用户级）：

```powershell
[System.Environment]::SetEnvironmentVariable("MYSQL_PASS", "MySQLpassword", "User")
[System.Environment]::SetEnvironmentVariable("MYSQL_DATABASES", "usergroup,papergroup,newsgroup,comments_and_ratings,feedbackgroup,administratorgroup,aigroup,analyticsgroup,institutiongroup,messagegroup,noticegroup,notificationgroup,qagroup", "User")
[System.Environment]::SetEnvironmentVariable("MYSQL_WRITABLE_DATABASES", "papergroup,usergroup", "User")
```

Git Bash：

```bash
echo 'export MYSQL_PASS=MySQLpassword' >> ~/.bashrc
echo 'export MYSQL_DATABASES=usergroup,papergroup,newsgroup,...' >> ~/.bashrc
source ~/.bashrc
```

## 接入 Claude Code

把这段塞进 `~/.claude.json` 的 `mcpServers` 块（**先保留**旧 13 个 `mysql-xxxgroup`，等验证通过再删）：

```jsonc
"mysql-router": {
  "type": "stdio",
  "command": "uv",
  "args": [
    "--directory", "D:\\claude\\mysql-router-mcp",
    "run", "mysql-router-mcp"
  ],
  "env": {}  // 真正的连接信息从环境变量读
}
```

然后 `exit` 再重启 Claude Code，新的 `mcp__mysql-router__mysql_query` 工具就会出现。

## 使用示例

```
# 读：列出 usergroup 的 users 表前 10 条
mcp__mysql-router__mysql_query(
  database="usergroup",
  sql="SELECT id, name, created_at FROM users ORDER BY id LIMIT 10"
)

# 参数化：避免注入
mcp__mysql-router__mysql_query(
  database="papergroup",
  sql="SELECT * FROM papers WHERE id = %s",
  params=[42]
)

# 写（需要 MYSQL_WRITABLE_DATABASES 包含该库）
mcp__mysql-router__mysql_query(
  database="usergroup",
  sql="UPDATE users SET last_login = NOW() WHERE id = %s",
  params=[42]
)
```

会被拒绝的请求（默认安全策略）：

* `database` 不在 `MYSQL_DATABASES` → 拒绝
* `DROP TABLE users` → 拒绝（DDL 黑名单）
* `UPDATE` 写到 `newsgroup`（不在 `MYSQL_WRITABLE_DATABASES`） → 拒绝
* `SELECT 1; DROP TABLE x` → 拒绝（多语句）

## 验证清单

1. `uv run mysql-router-mcp` 应该打印 starting 日志并等待 stdio
2. Claude Code 里跑 `mcp__mysql-router__mysql_query(database="usergroup", sql="SELECT 1")`，应该返回 `{"row_count": 1, ...}`
3. 跑 `... sql="DROP TABLE x"`，应该报 `DDL/DCL statement not allowed`
4. 验证通过后再删除旧 13 个 `mysql-xxxgroup` 条目