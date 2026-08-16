# 生产化改造笔记（Production Hardening Notes）

> 本文档记录把教学版「深度研搜」向可上线系统推进的**第一批安全加固**：
> 每一项都包含「原问题 → 风险 → 修复方式 → 验证方式」，并附尚未完成的企业级差距清单。
> 原始教学代码来自 [didilili/deepsearch-agents](https://github.com/didilili/deepsearch-agents)，
> 本仓库的增量改造以本文档为准，可作为代码评审和面试讲述的材料。

## 一、已完成的修复

### 1. 文件工具路径穿越（工具层）

- **原问题**：`resolve_path` 对不在会话目录内的绝对路径"保持原样"放行；含 `updated/` 的路径完全脱离会话约束直接 resolve。模型可以让 `read_file_content` 读取服务器任意文件（包括 `.env` 中的 API Key），让 `generate_markdown` 写任意路径。
- **风险**：任意文件读 / 任意文件写（CWE-22 Path Traversal）。提示词里"禁止使用绝对路径"属于软约束，被模型或提示注入绕过时没有任何代码层兜底。
- **修复**（`app/utils/path_utils.py` 重写）：
  - 所有路径解析结果强制收容（containment）在 `session_dir` 内，越界抛 `PathEscapeError`；
  - 绝对路径（Windows 盘符 / Unix 根路径）一律拒绝，要求模型改用相对路径；
  - 删除 `updated/` 旁路分支，历史路径形式折叠为会话目录内文件名；
  - 三个文件工具捕获 `PathEscapeError` 并返回引导性错误文本，模型可自我纠正重试，不中断 Agent 循环。
- **验证**：`tests/test_path_safety.py`（16 个用例：穿越、盘符路径、`/etc/passwd`、updated 旁路、空文件名等全部拒绝；正常相对路径、虚拟前缀、冗余层级压平正常工作）。

### 2. SQL 任意执行与表名注入（工具层）

- **原问题**：`execute_sql_query` 直接 `cursor.execute(模型生成的SQL)` 且 `autocommit=True`，模型生成 `DROP TABLE` / `UPDATE` 会真实执行；`get_table_data` 把表名直接拼进 SQL 字符串。
- **风险**：破坏性 SQL 执行、SQL 注入（CWE-89 / CWE-99）。
- **修复**（`app/tools/db_tools.py`）：
  - 新增 `assert_readonly_sql`：剥离注释后仅放行 `SELECT / SHOW / DESCRIBE / EXPLAIN` 单条语句；多语句（stacked queries）、`INTO OUTFILE/DUMPFILE`、`LOAD_FILE`、DML/DDL 关键字一律拒绝；
  - 新增 `validate_table_name`：表名白名单 `^[A-Za-z0-9_]{1,64}$` + 反引号包裹；
  - 被拒绝的 SQL 不建立数据库连接，直接返回错误文本。
- **验证**：`tests/test_sql_guard.py`（24 个用例：注释伪装、多语句注入、`load_file` 读敏感文件、反引号逃逸等全部拦截）。
- **部署配套**（账号层兜底，需在数据库侧执行）：
  ```sql
  CREATE USER 'deepsearch_ro'@'%' IDENTIFIED BY '<强密码>';
  GRANT SELECT ON deepsearch_db.* TO 'deepsearch_ro'@'%';
  ```
  `.env` 中 `MYSQL_USER` 改用该只读账号。工具层校验是第一道防线，数据库最小权限是第二道。

### 3. API 层加固（服务层）

均位于 `app/api/server.py`：

| # | 原问题 | 风险 | 修复 |
|---|--------|------|------|
| 3.1 | `thread_id` 客户端可控且直接拼进目录名 | `thread_id=../../x` 拼出任意目录（CWE-22） | 全部入口（task/cancel/upload/files/download/ws）统一 `^[A-Za-z0-9_-]{1,64}$` 白名单校验 |
| 3.2 | 上传无扩展名校验、无大小限制、`file.filename` 直接拼路径 | 任意类型任意大小文件写入、穿越写（CWE-434/22）、磁盘耗尽 | 扩展名白名单（与读取工具支持格式一致）、单文件大小上限（`MAX_UPLOAD_SIZE_MB`，默认 20MB）、文件名清洗只保留 basename、aiofiles 异步分块写入、超限清理半成品 |
| 3.3 | `/api/files` 和 `/api/download` 接受客户端传入的任意绝对路径，只校验"在 output 内" | 任何客户端可枚举并下载**所有其他会话**的报告与上传附件（横向越权，CWE-862/863） | 接口只接受 `thread_id`（+会话内相对路径），目录由服务端拼接，天然隔离会话；相对路径 resolve 后再做收容校验 |
| 3.4 | `allow_origins=["*"]` 且 `allow_credentials=True` | 任意网站可携带凭据跨域调用（CWE-942） | CORS 收敛为 `CORS_ORIGINS` 环境变量配置的白名单，默认仅本地 Vite 开发端口 |
| 3.5 | 错误以 200 + `{"error": ...}` 返回 | 前端与监控无法区分错误，掩盖故障 | 统一 `HTTPException` 返回 400/404/413/422/500 正确状态码 |
| 3.6 | 无健康检查 | 容器/负载均衡无法探活 | 新增 `GET /health` |
| 3.7 | 任务文本无长度限制 | 超长输入直接打满模型上下文（成本攻击面） | `query` 限制 1–10000 字符（Pydantic Field） |

- **验证**：`tests/test_api_security.py`（36 个用例，`run_deep_agent` monkeypatch 为空协程，不发起真实 LLM 调用）。

### 4. 前端适配

`/api/files`、`/api/download` 改为 thread_id 驱动后，前端 `api.ts` / `useDeepAgentSession.ts` / `FileDock` / `ConversationThread` / `types.ts` 同步修改：文件列表返回会话内相对路径 + `thread_id`，下载时由服务端定位会话目录。`tsc -b` 类型检查通过。

## 二、如何验证

```bash
# 后端安全测试（76 个用例）
uv sync --group dev
uv run pytest tests/ -q

# 前端类型检查
cd frontend && pnpm install && pnpm exec tsc -b
```

## 三、尚未完成的企业级差距（下一步路线图）

以下按「上线阻塞性」排序，是诚实的能力边界，也是后续迭代计划：

1. **认证与多租户（上线前必须）**：当前所有接口无认证。最小方案：API Key / JWT Bearer 认证中间件 + 按用户隔离会话目录（`output/{user_id}/session_{thread_id}`）+ 会话归属校验。涉及用户体系时对接 OIDC（企业常见 Authing / Casdoor / 自建）。
2. **限流与成本控制（上线前必须）**：`slowapi` 按 IP/用户限流；DeepAgents 已有模型调用次数限制中间件（见 `examples/13-model-call-limit-middleware.py`），接入主 Agent；LLM 调用计入预算熔断。
3. **状态持久化（上线前必须）**：`InMemorySaver` 重启丢会话、多副本不共享。替换为 `langgraph-checkpoint-postgres`（或 SQLite 起步）；`active_tasks` 进程内 dict 改 Redis 或任务表，否则多 worker 部署下取消接口直接失效。
4. **任务队列与并发治理**：当前 `asyncio.create_task` 进程内执行，重启即丢、无法水平扩容。引入 Celery / ARQ / Temporal，Agent 执行与 API 进程分离。
5. **可观测性**：`print` 全量替换为结构化日志（logging + JSON formatter），接入 OpenTelemetry trace（LangSmith / LangFuse 追踪 Agent 链路），Prometheus 指标（任务时长、工具失败率、LLM token 消耗）+ 告警。
6. **评测体系**：固定评测集（20–50 个典型任务）+ LLM-as-judge 自动评分 + 检索质量（命中率/引用准确率）指标，接 CI 做回归。这是 Agent 项目区别于 demo 的核心证据。
7. **CI/CD**：GitHub Actions（lint + pytest + tsc + build），pre-commit 已有配置需补齐 ruff/mypy 钩子；后端目前无 Dockerfile（只有 MySQL compose），需补多阶段构建镜像。
8. **内容安全**：上传文件病毒扫描（ClamAV）、模型输出审核（涉政/敏感词）、提示注入防护（系统提示与用户输入隔离、工具结果标记为不可信数据）。

## 四、与上游教学版的关系

本仓库基于开源教学项目 deepsearch-agents（MIT 协议）。简历与面试中的正确定位是：
"基于开源教学项目做了**生产化安全改造**：修复路径穿越、SQL 任意执行、横向越权、
无限制上传等 7 类问题，补齐 76 个安全回归测试"——而不是把整个项目说成从零自研。
能逐条讲清楚"原版哪里有洞、我怎么修的、怎么验证的"，比笼统的"独立开发"更可信。
