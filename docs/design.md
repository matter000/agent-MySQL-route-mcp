# MySQL Router MCP — 设计文档

日期：2026-06-24
状态：✅ 已批准方案 A + ✅ 已批准方案 α（lazy-load 双进程）

> 增量 v2：用户要求"只有需要用到数据库的时候才调用这个 mcp"，新增 Proxy + Backend 双进程架构，原 backend 实现不变。

## 背景

`C:\Users\18500\.claude\.claude.json` 中配置了 **13 个** MySQL MCP 实例（administratorgroup / aigroup / analyticsgroup / comments_and_ratings / feedbackgroup / institutiongroup / messagegroup / newsgroup / noticegroup / notificationgroup / papergroup / qagroup / usergroup），全部基于 `@benborla29/mcp-server-mysql`，区别仅在 `MYSQL_DB` 字段。

主要问题：
1. 13 个独立 stdio 进程，启动开销大
2. 命名空间被 13 个 `mcp__mysql-xxx__mysql_query` 污染
3. 同一密码以明文形式被复制 13 次（违反项目 CLAUDE.md 红线 #1）
4. 所有库都默认开了 DDL 权限，安全风险高

## 目标

用一个 MCP server 取代全部 13 个实例：
- 工具签名带 `database` 参数，从源头杜绝选错库
- 密码从环境变量读取，配置里只留 `${MYSQL_ROOT_PASSWORD}` 占位
- 默认只读，按库白名单放开写权限
- 单一连接池，13 个 schema 复用

## 架构

```
Claude Code (stdin/stdout JSON-RPC)
       │
       ▼
mysql-router-mcp (Python, stdio server)
       │
       ├─ aiomysql 连接池  ───► MySQL 127.0.0.1:3306
       │                            ├─ usergroup
       │                            ├─ papergroup
       │                            ├─ newsgroup
       │                            └─ ... (共 13 个 db)
       │
       └─ 只读拦截器（默认拒绝 INSERT/UPDATE/DELETE/DDL）
```

## 接口

只暴露一个工具 `mysql_query`：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `database` | string | 是 | 必须在 `MYSQL_DATABASES` 白名单内 |
| `sql` | string | 是 | 单条 SQL；多条会被拒绝 |
| `params` | list | 否 | 参数化绑定，防注入 |

返回：`{"rows": [...], "row_count": N, "database": "...", "elapsed_ms": X}`

## 安全策略

1. **白名单**：启动时从 `MYSQL_DATABASES` 读合法 db 列表，请求里 `database` 不在列表直接拒绝
2. **只读默认**：`MYSQL_DEFAULT_READ_ONLY=true` 时，所有非 SELECT 语句拒绝
3. **按库放开**：可用 `MYSQL_WRITABLE_DATABASES=papergroup,usergroup` 在白名单内再开一批可写库
4. **DDL 黑名单**：`DROP / TRUNCATE / GRANT / REVOKE / ALTER` 即使在可写库也拒绝（除非显式 `MYSQL_ALLOW_DDL=true`）
5. **多语句拒绝**：检测 `;` 后非空白字符 >0 的情况，拒绝批量执行
6. **超时**：单条 SQL `MAX_EXEC_SECONDS` 默认 30s
7. **结果集上限**：`MAX_ROWS` 默认 1000

## 配置项（环境变量）

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `MYSQL_HOST` | 是 | — | MySQL host |
| `MYSQL_PORT` | 否 | 3306 | 端口 |
| `MYSQL_USER` | 是 | — | 用户名 |
| `MYSQL_PASS` | 是 | — | 密码（注入真实值，不写进 `~/.claude.json`） |
| `MYSQL_DATABASES` | 是 | — | 逗号分隔白名单 |
| `MYSQL_DEFAULT_READ_ONLY` | 否 | true | 全局默认只读 |
| `MYSQL_WRITABLE_DATABASES` | 否 | 空 | 白名单内可写的 db 列表 |
| `MYSQL_ALLOW_DDL` | 否 | false | 是否放开 DDL（强烈建议 false） |
| `MYSQL_MAX_ROWS` | 否 | 1000 | 单次返回行数上限 |
| `MYSQL_MAX_EXEC_SECONDS` | 否 | 30 | 单条 SQL 超时 |
| `MYSQL_POOL_MIN` | 否 | 1 | 连接池最小 |
| `MYSQL_POOL_MAX` | 否 | 5 | 连接池最大 |

## 文件清单

| 路径 | 作用 |
|---|---|
| `D:\claude\mysql-router-mcp\pyproject.toml` | uv 项目元数据 |
| `D:\claude\mysql-router-mcp\src\mysql_router_mcp\__init__.py` | 包标记 |
| `D:\claude\mysql-router-mcp\src\mysql_router_mcp\server.py` | MCP server 主逻辑 |
| `D:\claude\mysql-router-mcp\README.md` | 安装 + 配置说明 |
| `D:\claude\mysql-router-mcp\.env.example` | 环境变量样例 |

## 实施步骤

1. `uv init` 项目骨架
2. `uv add mcp aiomysql` 装依赖
3. 实现 `server.py`（连接池 + 工具 + 安全拦截）
4. 本地启动，对 13 个 db 各跑一次 `SELECT 1` 验证
5. 输出改造后的 `~/.claude.json` 片段（删除 13 个旧条目 + 新增 mysql-router）
6. 用户手动设置 `MYSQL_ROOT_PASSWORD` 环境变量 → 重启 Claude Code 验证

## 非目标

- 不做 SQL 美化 / EXPLAIN / schema 可视化（YAGNI）
- 不做 MySQL 8 之外的版本兼容测试（只支持 8.x）
- 不做 TLS / SSH 隧道（本地 127.0.0.1 直连）
- 不动现有的 `@benborla29/mcp-server-mysql`（保留在 `~/.claude.json` 直到用户验证通过再删除）

---

# 增量 v2：Lazy-load Proxy + Backend 双进程（方案 α）

## 动机

启动 Claude Code 时即使完全不查数据库，backend 也已建好 aiomysql 连接池，进程常驻到会话结束。浪费资源。

## 目标

- 启动 Claude Code → **零数据库开销**
- 第一次 `mcp__mysql-router__mysql_query(...)` → proxy 才 spawn backend
- 空闲 N 秒 → backend 自动 kill，下次再起

## 架构

```
Claude Code / Codex ──stdio──► mysql-router-proxy  (永远在线, ~150 行)
                                    │
                                    │ 第一次 call_tool 时 spawn
                                    ▼
                              mysql-router-mcp (backend, 已有, 0 改动)
                                    │
                                    └─ 空闲 60s 后 kill, 下次用再起
```

## Proxy 行为表

| 阶段 | proxy 干啥 | backend 跑没跑 |
|---|---|---|
| Claude Code 启动 | 注册静态工具、监听 stdio | ❌ |
| `tools/list` | 返回静态 `mysql_query` 定义 | ❌ |
| **第一次** `tools/call` | spawn backend + 走 initialize 握手 + 转发 | ✅ |
| 后续 `tools/call` | 直接转发 | ✅ |
| 空闲 ≥ `MYSQL_ROUTER_IDLE_TIMEOUT` | reaper 线程 kill backend | ❌ |
| 下次再 call | 重新 cold-start backend | ✅ |

## 关键实现细节

1. **静态 `tools/list`**：proxy 注册一个静态 `mysql_query` 工具定义，**不** spawn backend
2. **initialize 握手**：第一次 `tools/call` 时先 spawn backend → 发 `initialize` 请求 → 等响应 → 发 `notifications/initialized` → 再转发真正的 `tools/call`
3. **JSON-RPC 透传**：proxy 给 backend 的请求带 `id`，响应也带 `id`，proxy 不修改 id
4. **stderr drain**：backend 的 stderr 输出（INFO 日志）由 proxy 转发到自己的 stderr，避免 PIPE 满阻塞
5. **reaper 单例**：每个 backend 进程最多一个 reaper 协程；backend 被 kill 后 reaper 退出，下次 spawn 时再起新的
6. **cold-start 超时**：`MYSQL_ROUTER_STARTUP_TIMEOUT` 默认 30s，包含 backend 启动 + aiomysql 建池 + initialize 握手

## 新增文件

| 路径 | 作用 |
|---|---|
| `D:\claude\mysql-router-mcp-proxy\pyproject.toml` | uv 项目元数据（仅依赖 mcp SDK） |
| `D:\claude\mysql-router-mcp-proxy\src\mysql_router_proxy\__init__.py` | 包标记 |
| `D:\claude\mysql-router-mcp-proxy\src\mysql_router_proxy\proxy.py` | 主逻辑（~150 行） |
| `D:\claude\mysql-router-mcp-proxy\README.md` | 说明 |

backend (`mysql-router-mcp`) **一行不改**。

## 新增环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `BACKEND_DIR` | `D:\claude\mysql-router-mcp` | backend 工作目录 |
| `BACKEND_CMD` | `uv` | backend 启动命令 |
| `BACKEND_ARGS` | `run mysql-router-mcp` | backend 启动参数 |
| `MYSQL_ROUTER_IDLE_TIMEOUT` | `60` | 空闲 N 秒后 kill backend |
| `MYSQL_ROUTER_STARTUP_TIMEOUT` | `30` | spawn + initialize 超时（秒） |
| `MYSQL_ROUTER_PROXY_LOG_LEVEL` | `INFO` | proxy 日志级别 |

## 改造面

| 文件 | 操作 |
|---|---|
| `C:\Users\18500\.claude.json` | 删 9 个 mysql-xxxgroup + 加 mysql-router 指向 proxy |
| `C:\Users\18500\.codex\config.toml` | 删 10 个 mysql-xxxgroup + 加 mysql-router 指向 proxy |
| `C:\Users\18500\.bashrc` (用户手动) | 加 `MYSQL_PASS=...` 等环境变量一次 |

## 剩余未定位的 3 个 MCP

`mysql-analyticsgroup` / `mysql-notificationgroup` / `mysql-qagroup` 未在 `.claude.json` 或 `.codex/config.toml` 中找到。若存在第三个配置文件，用户需补充告知。