# 生产化改造笔记（Production Hardening Notes）

> 本文档记录把教学版「深度研搜」向可上线系统推进的改造过程：
> 每一项都包含「原问题 → 风险 → 修复方式 → 验证方式」，并附尚未完成的企业级差距清单。
> 原始教学代码来自 [didilili/deepsearch-agents](https://github.com/didilili/deepsearch-agents)，
> 本仓库的增量改造以本文档为准，可作为代码评审和面试讲述的材料。
>
> - 第一批（安全加固）：路径穿越、SQL 只读、上传限制、横向越权、CORS 等 7 类问题
> - 第二批（认证与状态）：API Key 认证 + 多租户隔离 + SQLite 持久化 checkpointer
> - 第三批（限流与成本）：slowapi 按密钥限流 + fail-closed 开发模式 + 模型调用硬上限
> - 第四批（P0-3 事件回放 + P1-4 任务硬超时）：SQLite 事件库 + seq 序号 + last_seq 差量补发 + 前端指数退避；`TASK_TIMEOUT_SECONDS` 任务级硬超时
> - 审查修复：第三批后审查修复 4 项（上传总量限制、失败回滚、测试隔离、非 ASCII 密钥）；第四批后审查修复 3 项（前端补发预算烧穿、差量补发截断、双 commit）+ 清理死代码 1 项

## 一、已完成的修复

### 1. 文件工具路径穿越（工具层）

- **原问题**：`resolve_path` 对不在会话目录内的绝对路径"保持原样"放行；含 `updated/` 的路径完全脱离会话约束直接 resolve。模型可以让 `read_file_content` 读取服务器任意文件（包括 `.env` 中的 API Key），让 `generate_markdown` 写任意路径。
- **风险**：任意文件读 / 任意文件写（CWE-22 Path Traversal）。提示词里"禁止使用绝对路径"属于软约束，被模型或提示注入绕过时没有任何代码层兜底。
- **修复**（`app/utils/path_utils.py` 重写）：
  - 所有路径解析结果强制收容（containment）在 `session_dir` 内，越界抛 `PathEscapeError`；
  - 绝对路径（Windows 盘符 / Unix 根路径）一律拒绝，要求模型改用相对路径；
  - 删除 `updated/` 旁路分支，历史路径形式折叠为会话目录内文件名；
  - 三个文件工具捕获 `PathEscapeError` 并返回引导性错误文本，模型可自我纠正重试，不中断 Agent 循环。
- **验证**：`tests/test_path_safety.py`（14 个用例：穿越、盘符路径、`/etc/passwd`、updated 旁路、空文件名等全部拒绝；正常相对路径、虚拟前缀、冗余层级压平正常工作）。

### 2. SQL 任意执行与表名注入（工具层）

- **原问题**：`execute_sql_query` 直接 `cursor.execute(模型生成的SQL)` 且 `autocommit=True`，模型生成 `DROP TABLE` / `UPDATE` 会真实执行；`get_table_data` 把表名直接拼进 SQL 字符串。
- **风险**：破坏性 SQL 执行、SQL 注入（CWE-89 / CWE-99）。
- **修复**（`app/tools/db_tools.py`）：
  - 新增 `assert_readonly_sql`：剥离注释后仅放行 `SELECT / SHOW / DESCRIBE / EXPLAIN` 单条语句；多语句（stacked queries）、`INTO OUTFILE/DUMPFILE`、`LOAD_FILE`、DML/DDL 关键字一律拒绝；
  - 新增 `validate_table_name`：表名白名单 `^[A-Za-z0-9_]{1,64}$` + 反引号包裹；
  - 被拒绝的 SQL 不建立数据库连接，直接返回错误文本。
- **验证**：`tests/test_sql_guard.py`（45 个用例：注释伪装、多语句注入、`load_file` 读敏感文件、反引号逃逸、SELECT 无 LIMIT 自动追加、UNION 等集合操作补 LIMIT 等全部拦截）。
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

- **验证**：`tests/test_api_security.py`（28 个用例，`run_deep_agent` monkeypatch 为空协程，不发起真实 LLM 调用）。

### 4. 前端适配

`/api/files`、`/api/download` 改为 thread_id 驱动后，前端 `api.ts` / `useDeepAgentSession.ts` / `FileDock` / `ConversationThread` / `types.ts` 同步修改：文件列表返回会话内相对路径 + `thread_id`，下载时由服务端定位会话目录。`tsc -b` 类型检查通过。

## 二、第二批：认证、多租户隔离与状态持久化

### 5. API Key 认证（`app/api/auth.py` 新增）

- **原问题**：所有接口无认证，任何知道地址的人都能启动任务（消耗 LLM API 费用）、上传文件、拉取产物。
- **修复**：
  - `API_KEYS=用户名:密钥` 环境变量配置租户密钥（密钥 ≥16 位，常数时间比较防时序侧信道）；
  - 所有 `/api/*` 接口经 `X-API-Key` 请求头认证（FastAPI 依赖注入），错误密钥 401，配置格式错误 500；
  - 浏览器直链（下载）与 WebSocket 无法自定义请求头，回退 `api_key` 查询参数（已知取舍：查询串可能进入访问日志）；
  - 不配置 `API_KEYS` 时为本地开发模式（归属默认用户 `local`，不鉴权），保证教学场景零配置可用；`/health` 免鉴权供探活。
- **验证**：`tests/test_auth.py`（密钥解析、401/500 行为、WS 握手拒绝）。

### 6. 多租户隔离（server.py + main_agent.py）

- **原问题**：会话目录、上传目录、任务表、WebSocket 推送全部只按 `thread_id` 区分。不同用户使用相同 thread_id 时：A 能列举下载 B 的产物、A 能取消 B 的任务、monitor 事件会推给错误的连接（租户间信息串台）。
- **修复**：
  - 认证后将每个请求绑定到 `Principal.user_id`；
  - 目录结构改为 `output/user_{uid}/session_{tid}`、`updated/user_{uid}/session_{tid}`；
  - `active_tasks`、WebSocket 路由、LangGraph thread_id 统一使用复合键 `{user_id}-{thread_id}`（user_id 限纯小写字母数字，保证复合键无歧义；thread_id 上限收到 48 位，复合后 ≤64）；
  - 隔离由构造保证：文件接口的服务端路径拼接天然只落到当前租户目录，无需逐条授权判断。
- **验证**：`tests/test_auth.py::TestTenantIsolation`（同 thread_id 跨租户 404、上传落位租户目录）。

### 7. 会话状态持久化（main_agent.py）

- **原问题**：`InMemorySaver` 作为 checkpointer——服务重启丢全部会话上下文，多副本部署状态不共享，"断点续聊"不存在。
- **修复**：
  - 替换为 `AsyncSqliteSaver`（`langgraph-checkpoint-sqlite`），持久化到 `CHECKPOINT_DB`（默认 `app/data/checkpoints.sqlite3`，已入 .gitignore）；
  - Agent 改为**惰性初始化**（首次任务时建库建连接并缓存复用），避免模块导入即占连接；
  - 服务重启后同一复合 thread_id 自动恢复历史消息与执行状态。
- **验证**：`tests/test_checkpointer.py`（checkpoint 写入后由**新 saver 实例**（等价重启）读回；thread 间隔离；惰性初始化建库且缓存复用）。
- **边界说明**：SQLite 适合单机部署；多副本水平扩容时换 `langgraph-checkpoint-postgres`，接口不变，只换 saver 实现。

## 三、第三批：限流、fail-closed 与模型调用硬上限

### 8. 接口限流（`server.py`，slowapi）

- **原问题**：认证挡住了陌生人，但一个合法密钥可以无限发任务（烧 LLM/Tavily 费用）、无限上传。
- **修复**：
  - `POST /api/task` 限 `RATE_LIMIT_TASK`（默认 10 次/分钟）、`POST /api/upload` 限 `RATE_LIMIT_UPLOAD`（默认 30 次/分钟）；
  - 限流键按**密钥哈希**（SHA-256 截断 16 位，明文不入限流状态与日志）计，无密钥（开发模式）回退客户端 IP；
  - 超限返回 429 + 剩余额度响应头；认证先于限流执行，401 请求不消耗配额。
- **已知边界**：反向代理后所有无密钥用户共享 IP；生产应确保客户端都携带密钥或配置可信代理解析。
- **验证**：`tests/test_rate_limit.py`（7 个用例：限流键构造、任务/上传超限 429、不同密钥独立配额、认证先于限流）。

### 9. 开发模式 fail-closed（`auth.py`）

- **原问题**：第二批的"未配置 API_KEYS 即开发模式"是 fail-open——部署时忘配密钥服务照常启动且完全开放。
- **修复**：未配置 `API_KEYS` 时业务接口默认返回 503；仅显式设置 `ALLOW_DEV_MODE=1` 才进入本地开发模式（归属 local 用户）；启动时 lifespan 打印配置状态（含醒目警告）。
- **验证**：`tests/test_auth.py::TestDevModeFailClosed`（未配置+未开启=503；显式开启=放行；`ALLOW_DEV_MODE=true` 等"看似开启"的值一律拒绝）。

### 10. 模型调用硬上限（`main_agent.py`，ModelCallLimitMiddleware）

- **原问题**：提示注入或规划失控可让模型无限循环烧 token；提示词里"最多检索 5 次"只是软约束。
- **修复**：`ModelCallLimitMiddleware(run_limit=80, thread_limit=300, exit_behavior="error")`——单次任务最多 80 次模型调用、单会话（跨重启累计）最多 300 次，超限抛异常由 `run_deep_agent` 捕获并经 monitor 告知前端。阈值可用 `MODEL_RUN_LIMIT` / `MODEL_THREAD_LIMIT` 环境变量调整。
- **边界**：这是调用次数上限，不是 token 预算；token 级熔断（按 usage_metadata 累计）仍是待做项。

## 四、第四批（2026-08-30）：P0-3 事件回放

### 11. 事件持久化与序号（`app/api/event_store.py` 新增 + `monitor.py` 改造）

- **原问题**：monitor 事件直接 `WS.send_json()`，发完即丢。断线期间的事件（工具调用、子智能体结果、**最终答案**）永久丢失；事件无序号，前端无法发现丢件；服务重启后历史事件无法回放给重连的前端；前端固定 2s 重连无退避（`useDeepAgentSession.ts`），服务抖动时雪崩重连。
- **修复**（协议与 Redis Stream 对齐，详见 `docs/EVENT_REPLAY_DESIGN.md`）：
  - `SqliteEventStore`：事件按复合键 `{user_id}-{thread_id}` 落 SQLite（`EVENT_DB` 可配，WAL 模式），`AUTOINCREMENT` 主键即全局自增事件序号 `seq`（等价 Stream ID）；`append` 写入时按 `EVENT_MAX_PER_STREAM`（默认 1000）裁剪旧事件（等价 `XADD MAXLEN`）；
  - `monitor._emit` 改为先落库拿 seq 再推 WS：payload 携带 `seq`，前端据此检测丢件；持久化失败降级为"该事件不可回放"，不阻塞实时推送；
  - WS 握手支持 `last_seq` 查询参数：服务端 `read_after` 只补发其后差量（等价 `XREAD`），补发完成后再注册实时推送（先注册会导致补发与实时事件交错、seq 乱序）；首次连接补发最近 `EVENT_REPLAY_LIMIT`（默认 100）条，页面刷新可恢复上一轮执行轨迹；非法 `last_seq` 拒绝握手（1008）；
  - 补发事件带 `replay: true` 标记：前端只对实时流做 seq 跳号检测，避免事件流被服务端裁剪后陷入"补发→再跳号→再补发"循环；
  - 前端：记录 `last_seq` 随重连上送；实时流 seq 跳号 → 主动断开重连触发差量补发（限 3 次预算）；重叠窗口重复事件按 seq 去重；固定 2s 重连改指数退避 + 随机抖动（2s→4s→…上限 60s），连接成功计数清零。
- **为什么是 SQLite 而非设计稿的 Redis Stream**：当前仍是单进程部署，引入 Redis 只为存事件不划算（第一性原理：先修真实缺陷，不提前引入用不上的基础设施）；`append`/`read_after` 与 `XADD`/`XREAD` 语义一一对应，P0-2 任务出进程接入 Redis 时只换存储实现类，monitor 与 WS 端点调用代码不变。
- **已知边界**：补发读取与实时注册之间存在毫秒级窗口，期间事件由前端 seq 跳号检测兜底（主动补发恢复）；WS 背压下推送顺序与 seq 顺序理论上可倒置（append 与 send 之间有让步点），同样由前端跳号检测→补发闭环自愈——协议把顺序视为不可信、以 seq 为准；多副本部署仍需 P0-2（事件广播），本批只解决单进程内的持久化与回放；设计稿的"任务结束 30 分钟后删除 Stream"未实现（只有 MAXLEN 条数裁剪，events 表随会话数增长，待办）。
- **验证**：`tests/test_event_replay.py`（14 个用例：seq 递增与 payload 结构、差量读取、task_key 流隔离、裁剪保留最近 N 条、新实例读回等价重启、monitor 落库携带 seq、last_seq 差量补发、首次连接恢复、实时事件带 seq、非法 last_seq 拒绝、跨租户回放隔离、差量全量补发不受 replay_limit 截断、并发 append 序号唯一有序）。

### 12. 任务级硬超时（`main_agent.py`，P1-4 的超时部分）

- **原问题**：LLM API 网络半开挂起时任务永远停在"运行中"——前端一直转圈、占用模型调用预算、普通取消难以打断；`run_deep_agent` 无外层超时。
- **修复**：流式消费逻辑抽取为 `_consume_agent_stream`，整个执行（含 Agent 惰性初始化）包在 `asyncio.wait_for(..., timeout=TASK_TIMEOUT_SECONDS)` 内，默认 600 秒可配。超时先取消内部协程再抛 `TimeoutError`，由 `run_deep_agent` 捕获并经 monitor 告知前端（错误事件同样落事件库、断线可回放）；用户主动取消仍走 `CancelledError` 分支。
- **验证**：`tests/test_checkpointer.py::TestTaskTimeout`（2 个用例：astream 挂起被超时终止并上报、初始化挂起同样被覆盖）。

### 13. 第四批代码审查修复（2026-08-30）

对第四批增量做正式代码审查后修复 3 项、清理 1 项：

| # | 问题 | 修复 |
|---|------|------|
| R-1 | 前端跳号触发重连后、连接真正关闭前，同一突发的每条实时事件都各自消耗一次补发预算（上限 3），3 条突发即烧穿，之后丢件不再补发 | resync pending 期间丢弃实时事件（重连补发会按序重投） |
| R-2 | `read_after(last_seq=...)` 误用首次连接的 `replay_limit`（100）做差量上限，断线累积更多事件时一轮补发补不完，多轮补发会烧穿预算 | 显式 last_seq 的差量上限放宽到 `max_per_stream`（裁剪天然边界），语义对齐 XREAD 读尽积压 |
| R-3 | 每条事件 commit 两次（INSERT 与裁剪 DELETE 各一次），多一次 WAL fsync | 合并为单事务单次提交 |
| R-4 | `ConnectionManager.connect` 改造后无调用方（死代码） | 删除，WS 端点统一 accept + register |

审查中证伪的怀疑（记录结论不修）：`asyncio.Lock` 跨事件循环绑定——探针实证 3.12 无争用快路径不绑定循环，生产单循环且测试无并发争用，不成立。

## 五、代码审查修复（2026-08-17）

对前三批全部增量做正式代码审查后修复的 4 项：

| # | 问题 | 修复 | 验证 |
|---|------|------|------|
| R-1 | 上传总量无上限：`MAX_UPLOAD_SIZE` 只限单文件，一次传 N 个文件可绕过（磁盘耗尽） | 新增请求级 `MAX_UPLOAD_TOTAL_SIZE`（默认 100MB）+ `MAX_UPLOAD_FILES`（默认 20），写盘前拒绝超量 | `test_too_many_files_rejected_before_write`、`test_total_size_limit_rolls_back_saved_files` |
| R-2 | 多文件上传非原子：第 N 个文件失败时前 N-1 个已落盘，留下部分上传状态 | `_rollback_saved`：任一文件校验/写入失败即清理本次已写入的全部文件 | `test_total_size_limit_rolls_back_saved_files`、`test_invalid_extension_rolls_back_previous_files` |
| R-3 | 非 ASCII 密钥头触发 `compare_digest` TypeError → 500（错误处理缺陷，可刷错误日志） | 非 ASCII 密钥直接按无效处理返回 401（不可能匹配任何合法密钥） | `test_non_ascii_key_rejected_not_500` |
| R-4 | 测试直接读写真实 `app/output`、`app/updated` 运行时目录（残留文件可能被真实任务读到） | 三个 API 测试文件加 autouse fixture，把 `output_dir`/`updated_dir` 指向 `tmp_path`；已清理历史残留 | 修复后全量测试跑完真实目录 0 文件 |

审查中记录但暂缓的低优先级项（见面试/03 缺陷清单）：`_agent_init_lock` 跨事件循环隐患（加注释/重构）、`rglob` 符号链接防御、`last_msg.content` 类型防御、前端 401 专门提示。

## 六、如何验证

```bash
# 后端全部测试（145 个用例：安全 90 + 认证/租户/限流 35 + 持久化与超时 6 + 事件回放 14）
# 分布：path_safety 14 / sql_guard 45 / api_security 31 / auth 28 / rate_limit 7 / checkpointer 6 / event_replay 14
uv sync --group dev
uv run pytest tests/ -q

# 前端类型检查
cd frontend && pnpm install && pnpm exec tsc -b
```

## 七、尚未完成的企业级差距（下一步路线图）

以下按「上线阻塞性」排序，是诚实的能力边界，也是后续迭代计划：

1. **任务队列与并发治理（上线前必须，唯一剩余 P0）**：当前 `asyncio.create_task` 进程内执行，重启即丢、无法水平扩容；`active_tasks` 是进程内 dict，多 worker 部署下取消接口失效。引入 Celery / ARQ / Temporal，Agent 执行与 API 进程分离，任务状态入 Redis 或数据库，事件广播换 Redis Stream（本批事件存储已按 append/read_after 可替换语义设计，替换只改实现类）。~~限流与成本控制~~（第三批完成）、~~事件回放单机版~~（第四批完成，多副本广播仍需本项）。
2. **可观测性**：`print` 全量替换为结构化日志（logging + JSON formatter），接入 OpenTelemetry trace（LangSmith / LangFuse 追踪 Agent 链路），Prometheus 指标（任务时长、工具失败率、LLM token 消耗）+ 告警。
3. **评测体系**：~~固定评测集~~（最小版已建：`eval/` 20 用例 + 确定性判分 + LLM-as-judge）；待扩到 50 条并接 CI 做回归。这是 Agent 项目区别于 demo 的核心证据。
4. **CI/CD**：GitHub Actions（lint + pytest + tsc + build），pre-commit 已有配置需补齐 ruff/mypy 钩子；后端目前无 Dockerfile（只有 MySQL compose），需补多阶段构建镜像。
5. **内容安全**：上传文件病毒扫描（ClamAV）、模型输出审核（涉政/敏感词）、提示注入防护（系统提示与用户输入隔离、工具结果标记为不可信数据）。
6. **认证升级路径**：当前 API Key 适合个人/小团队部署；对外多用户产品需换 OIDC（Authing / Casdoor / Auth0）+ JWT，密钥轮换与吊销机制（短时一次性令牌替代查询参数密钥）。

## 八、与上游教学版的关系

本仓库基于开源教学项目 deepsearch-agents（MIT 协议）。简历与面试中的正确定位是：
"基于开源教学项目做了**生产化改造**：四批改造 + 两次正式代码审查修复，覆盖路径穿越、
SQL 任意执行、横向越权、无认证、租户串台、无限流、事件不可回放、任务无超时等 16 类问题，145 个回归测试"——
而不是把整个项目说成从零自研。
能逐条讲清楚"原版哪里有洞、我怎么修的、怎么验证的"，比笼统的"独立开发"更可信。
