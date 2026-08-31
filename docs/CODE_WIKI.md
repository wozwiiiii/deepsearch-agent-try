# deepsearch-agents Code Wiki

> 生成基准：`production-hardening` 分支（2026-08-30，HEAD `2a0db27`），154 个测试全绿。
> 本文档面向需要快速理解代码结构的开发者与评审者；改造动机与"原问题→修复→验证"明细见 [PRODUCTION_NOTES.md](PRODUCTION_NOTES.md)，本文聚焦**现状结构**。

---

## 目录

1. [项目概览](#1-项目概览)
2. [整体架构](#2-整体架构)
3. [目录结构](#3-目录结构)
4. [模块详解：api 层](#4-模块详解api-层)
5. [模块详解：agent 层](#5-模块详解agent-层)
6. [模块详解：tools 层](#6-模块详解tools-层)
7. [模块详解：utils 与 prompt](#7-模块详解utils-与-prompt)
8. [前端架构](#8-前端架构react--vite--antd)
9. [评测体系 eval](#9-评测体系-eval)
10. [基础设施与配置](#10-基础设施与配置)
11. [依赖关系总览](#11-依赖关系总览)
12. [运行方式](#12-运行方式)
13. [测试体系](#13-测试体系)
14. [当前能力边界与演进路线](#14-当前能力边界与演进路线)

---

## 1. 项目概览

**定位**：多智能体深度研究系统（Deep Research Agent）。用户提交一个研究任务，主智能体调度三个专职子智能体从四种数据源（公网 / MySQL / RAGFlow 知识库 / 上传文件）检索信息，产出 Markdown/PDF 报告，全过程经 WebSocket 实时推送前端。

**与上游的关系**：底座是开源教学项目 [didilili/deepsearch-agents](https://github.com/didilili/deepsearch-agents)（MIT）。`main` 分支与上游对齐；本仓库的全部增量在 `production-hardening` 分支（四批生产化改造 + 两次代码审查修复，`git diff main` 可查），核心是**安全加固、认证/多租户、限流/成本、事件回放**。

**技术栈**：

| 层 | 技术 |
|----|------|
| Agent 框架 | DeepAgents 0.5.7 + LangGraph 1.1.10 + LangChain 1.2.17 |
| LLM 接入 | OpenAI 兼容协议（`init_chat_model`，实际为阿里云 DashScope / qwen-max） |
| 后端 | FastAPI + uvicorn，slowapi（限流），aiosqlite（事件库/checkpointer） |
| 工具客户端 | tavily-python（搜索）、mysql-connector-python、ragflow-sdk、pypdf/python-docx/openpyxl |
| 前端 | React 19 + TypeScript + Vite + Ant Design 5 + pnpm |
| 数据 | MySQL 8.4（Docker，教学数据：药品 50 / 库存 150 / 销售记录 100 条） |

---

## 2. 整体架构

### 2.1 分层图

```
┌──────────────────────────────────────────────────────────────────┐
│ 前端 React (Vite dev :5173)                                       │
│  App.tsx → useDeepAgentSession（WS 会话状态机）→ components/*     │
└───────┬──────────────────────────────────▲───────────────────────┘
        │ HTTP (X-API-Key 头)              │ WebSocket (token/last_seq)
┌───────▼──────────────────────────────────┴───────────────────────┐
│ api 层  server.py（接口/上传/下载/限流/WS 端点）                    │
│         auth.py（API Key 认证 + 租户 + 短时令牌）                  │
│         monitor.py（事件总线：落库→推送）                          │
│         event_store.py（SQLite 事件库，seq 差量回放）              │
│         context.py（ContextVar：session_dir/thread_id）           │
├──────────────────────────────────────────────────────────────────┤
│ agent 层 main_agent.py（组装 DeepAgent + 执行 + 超时/token 预算）  │
│           llm.py（模型单例） prompts.py（YAML 加载）               │
│           subagents/（三个字典式子智能体配置）                     │
├──────────────────────────────────────────────────────────────────┤
│ tools 层 tavily_tool / db_tools / ragflow_tools                   │
│          markdown_tools / pdf_tools / upload_file_read_tool       │
├──────────────────────────────────────────────────────────────────┤
│ utils 层 path_utils（路径收容校验） word_converter（MD→PDF）       │
└──────────────────────────────────────────────────────────────────┘
         外部服务：LLM API │ Tavily │ MySQL │ RAGFlow
```

**分层契约**（AGENTS.md 约定）：api 层不碰业务，tools 层不关心调用方；工具返回错误字符串而不抛异常（异常会打断 Agent 循环，返回引导性文本让模型自我纠正重试）。

### 2.2 一次任务的完整生命周期

```
1. POST /api/task (X-API-Key)
   → 认证 → 限流(10/min) → validate_thread_id → 复合键 {user_id}-{thread_id}
   → asyncio.create_task(run_deep_agent(...)) → 立即返回 {"status":"started"}

2. run_deep_agent（后台协程，wait_for 600s 硬超时包裹）
   → 建 output/user_{uid}/session_{tid} 目录
   → 复制 updated/ 上传文件到 session 目录 → ContextVar 写入目录/路由键
   → monitor.report_session_dir（事件①：落库→推 WS）
   → agent.astream() 逐片段消费：
       模型消息 usage_metadata → 累计 token（超 150 万熔断）
       task 工具调用 → monitor.report_assistant（子智能体路由事件）
       最终文本 → monitor.report_task_result（结果事件）

3. 每条 monitor 事件：event_store.append（拿全局 seq）→ WS.send_json(payload+seq)

4. 前端 onmessage：seq 跳号→主动断开重连；WS 重连握手带 last_seq
   → server 先 read_after 差量补发（replay 标记）→ 再 register 实时推送
```

### 2.3 多租户复合键（贯穿全系统的隔离根）

`{user_id}-{thread_id}` 统一用于四处：`active_tasks` 字典键、WebSocket 路由键、LangGraph checkpointer thread_id、事件流 task_key。user_id 限 `^[a-z0-9]{1,15}$`（**禁连字符**，保证复合键按第一个连字符切分无歧义），thread_id 限 `^[A-Za-z0-9_-]{1,48}$`。目录隔离：`output/user_{uid}/session_{tid}`、`updated/` 同构——隔离由构造（路径拼接）保证，不靠逐处授权判断。

---

## 3. 目录结构

```
deepsearch-agents/
├── app/
│   ├── api/                    # ★ 接口层
│   │   ├── server.py           # FastAPI 入口：全部 HTTP/WS 端点、限流、上传/下载
│   │   ├── auth.py             # API Key 认证、Principal、短时链接令牌
│   │   ├── monitor.py          # ToolMonitor 事件总线 + ConnectionManager
│   │   ├── event_store.py      # SqliteEventStore（P0-3 事件回放）
│   │   └── context.py          # ContextVar 上下文（session_dir / thread_id）
│   ├── agent/                  # ★ 智能体层
│   │   ├── main_agent.py       # DeepAgent 组装 + run_deep_agent 执行入口
│   │   ├── llm.py              # init_chat_model 单例
│   │   ├── prompts.py          # prompts.yml 加载
│   │   └── subagents/          # 三个字典式子智能体配置
│   ├── prompt/prompts.yml      # 全部提示词（不在代码硬编码）
│   ├── tools/                  # ★ 工具层（LangChain @tool）
│   │   ├── tavily_tool.py      # internet_search
│   │   ├── db_tools.py         # list_sql_tables / get_table_data / execute_sql_query
│   │   ├── ragflow_tools.py    # get_assistant_list / create_ask_delete
│   │   ├── markdown_tools.py   # generate_markdown
│   │   ├── pdf_tools.py        # convert_md_to_pdf
│   │   └── upload_file_read_tool.py  # read_file_content（md/docx/pdf/xlsx）
│   ├── ragflow/                # RAGFlow 连接配置
│   └── utils/
│       ├── path_utils.py       # resolve_path 收容校验（安全核心）
│       └── word_converter.py   # MD→PDF 转换实现
├── frontend/                   # React 前端（见 §8）
├── tests/                      # 154 个测试（见 §13）
├── eval/                       # 评测集：cases / judge / runner（20 用例）
├── docker/                     # MySQL 8.4 compose + 初始化 SQL + 只读账号脚本
├── examples/                   # DeepAgents 框架 15 个教学脚本（非运行时依赖）
├── docs/                       # PRODUCTION_NOTES / 设计稿 / WORK_STATUS / 本 Wiki
├── .env.example                # 全部环境变量样例
└── pyproject.toml              # uv 管理的依赖（Python 3.12 锁定）
```

运行时产物（gitignore，勿提交）：`app/data/checkpoints.sqlite3`（会话状态）、`app/data/events.sqlite3`（事件库）、`app/output/`、`app/updated/`。

---

## 4. 模块详解：api 层

### 4.1 [server.py](../app/api/server.py) — 接口层核心

**端点一览**：

| 端点 | 方法 | 鉴权 | 限流 | 职责 |
|------|------|------|------|------|
| `/health` | GET | 无 | 无 | 探活 |
| `/api/task` | POST | 头 | 10/min | 启动后台 Agent 任务（create_task，立即返回） |
| `/api/task/{thread_id}/cancel` | POST | 头 | 无 | 取消任务（task.cancel + 1s 等待，返回 cancelled/cancelling） |
| `/api/token` | POST | 头 | 30/min | 签发 60s 短时链接令牌（P1-2） |
| `/api/upload` | POST | 头 | 30/min | 多文件上传（扩展名/单文件/总量/数量四重校验 + 失败回滚） |
| `/api/files` | GET | 头 | 无 | 列会话产物（只收 thread_id，服务端拼路径） |
| `/api/download` | GET | 头>token>api_key | 无 | 下载会话文件（收容校验双保险） |
| `/ws/{thread_id}` | WS | token>api_key | 无 | 实时事件推送 + last_seq 差量回放 |

**关键函数**：

- `composite_task_key(user_id, thread_id) -> str`：复合键生成，四处共用同一形式。
- `validate_thread_id(thread_id) -> str`：白名单校验，非法值 400。
- `user_scope_dir(base_dir, user_id, thread_id) -> Path`：租户目录拼接，隔离的构造保证。
- `_rate_limit_key(request) -> str`：限流键 = 密钥 SHA-256 截断 16 位（明文不入状态）；无密钥回退 IP。
- `require_principal_for_link(...)`：直链鉴权依赖，优先级 头 > 短时令牌 > api_key 查询参数（兼容旧入口）。
- `run_task`：同 thread_id 旧任务先 cancel 再起新任务；`active_tasks: dict[str, asyncio.Task]` 进程内登记（**P0-2 待出进程**）。
- `upload_files`：`sanitize_filename`（只留 basename）+ 扩展名白名单 + 分块写盘 + 超限中途清理 + `_rollback_saved` 整体回滚。
- `websocket_endpoint`：鉴权 → `last_seq` 校验（非负整数，非法 1008 拒绝）→ accept → **先补发差量、再 register**（防补发与实时交错乱序）→ 心跳 pong 循环。

**模块级常量**（导入时读取，测试经 conftest 注入）：`MAX_UPLOAD_SIZE_MB=20`、`MAX_UPLOAD_TOTAL_MB=100`、`MAX_UPLOAD_FILES=20`、`MAX_QUERY_LENGTH=10000`、`CORS_ORIGINS`（默认仅本地 Vite）。

### 4.2 [auth.py](../app/api/auth.py) — 认证与租户

| 成员 | 说明 |
|------|------|
| `Principal` (dataclass, frozen) | 请求租户身份，唯一字段 `user_id`；`is_dev_mode` 属性 |
| `parse_api_keys(raw) -> dict[str,str]` | 解析 `API_KEYS=用户名:密钥,...`；用户名白名单/密钥≥16 位/查重，格式错误抛 RuntimeError（→500） |
| `authenticate_api_key(api_key) -> Principal` | 核心：逐候选 `secrets.compare_digest` 常数时间比较（非 ASCII 直接 401，防 TypeError→500）；未配置 API_KEYS 且未开 ALLOW_DEV_MODE=1 → **503（fail-closed）** |
| `require_principal` | FastAPI 依赖：从 `X-API-Key` 头解析 |
| `issue_link_token(user_id) -> (token, ttl)` | P1-2：`secrets.token_urlsafe(32)` + 进程内登记表 `_link_tokens`（TTL=`LINK_TOKEN_TTL_SECONDS` 默认 60s，签发时顺手清理过期项） |
| `resolve_link_token(token) -> Principal` | 令牌校验；无效/过期统一 401 不区分原因（防探测） |

### 4.3 [monitor.py](../app/api/monitor.py) — 事件总线

- **`ToolMonitor`（单例）**：业务工具只调 `report_tool / report_assistant / report_task_result / report_task_cancelled / report_error / report_session_dir`，签名稳定。
- `_emit` 流程：构造 payload → 确定目标事件循环 → `_schedule_persist_and_send` 投递协程（同循环 create_task，跨线程 run_coroutine_threadsafe）→ `_persist_and_send`：**先 `event_store.append` 拿 seq → 再 WS 推送**（持久化失败只降级为"该事件不可回放"，不阻塞推送）。
- **`ConnectionManager`**：`active_connections: dict[task_key, WebSocket]` + 绑定主循环 `loop`；`register` 与 accept 分离（回放协议要求先补发后注册）；`disconnect` 只在连接实例匹配时删除（防误删同键新连接）。

### 4.4 [event_store.py](../app/api/event_store.py) — 事件回放存储（P0-3）

`SqliteEventStore`（全局单例 `event_store`），接口语义**对齐 Redis Stream**（P0-2 换 Redis 只改实现类）：

| 方法 | 对应 Redis | 说明 |
|------|-----------|------|
| `append(task_key, event_type, message, data) -> seq` | `XADD` | 全局自增 seq（AUTOINCREMENT）；写入加锁串行化保 seq 顺序；INSERT+裁剪单事务单 commit |
| `read_after(task_key, last_seq=None, limit=None)` | `XREAD` | 有 last_seq → 全部差量（上限 `max_per_stream`，裁剪天然边界）；无 → 最近 `replay_limit` 条；返回带 `replay: true` 标记 |
| `close()` | — | 关闭连接 |

参数：`EVENT_DB`（默认 `app/data/events.sqlite3`，WAL 模式）、`EVENT_MAX_PER_STREAM=1000`（等价 MAXLEN 裁剪）、`EVENT_REPLAY_LIMIT=100`。惰性初始化（首次读写才建库建连接，`asyncio.Lock` 双重检查）。

### 4.5 [context.py](../app/api/context.py) — 隐式上下文

`ContextVar` 保存 `session_dir` 与 `thread_id`（复合键），`run_deep_agent` 开始时 set、finally 里 reset。深层工具（markdown/pdf/read_file）无需层层传参即可 `get_session_context()` / `get_thread_context()`。**注意**：这是隐式依赖，工具需容忍返回 None（脚本调试场景）。

---

## 5. 模块详解：agent 层

### 5.1 [main_agent.py](../app/agent/main_agent.py)

**模块级配置**（env 可覆盖）：

| 常量 | 默认 | 作用 |
|------|------|------|
| `CHECKPOINT_DB` | `app/data/checkpoints.sqlite3` | 会话状态持久化 |
| `MODEL_RUN_LIMIT` / `MODEL_THREAD_LIMIT` | 80 / 300 | 模型调用次数硬上限（单任务/单会话累计） |
| `TASK_TIMEOUT_SECONDS` | 600 | 任务硬超时（LLM 网络半开兜底） |
| `MODEL_TOKEN_RUN_LIMIT` | 1,500,000 | 单任务 token 预算熔断 |

**关键函数**：

- `_get_agent() -> DeepAgent`：惰性初始化 + `_agent_init_lock` 双重检查；组装 `create_deep_agent(model, system_prompt, tools=[generate_markdown, convert_md_to_pdf, read_file_content], checkpointer=AsyncSqliteSaver, subagents=[三个子智能体], middleware=[ModelCallLimitMiddleware])`，首次调用建库并缓存复用。
- `_consume_agent_stream(agent_factory, message, config)`：流式消费协程——逐 chunk 解析模型节点；`task` 工具调用上报子智能体路由；最终文本上报结果；**循环内累计 `usage_metadata` token，超预算抛 RuntimeError 熔断**（检查在片段处理之前）。独立成协程是为了让 `wait_for` 能包住整个执行（异步生成器不能直接套）。
- `run_deep_agent(task_query, session_id, user_id="local")`：执行入口——建租户目录 → 复制上传文件 → 写 ContextVar → 上报 session_dir → `asyncio.wait_for(_consume_agent_stream(...), timeout)` → 异常分类：`TimeoutError`（上报超时）、`CancelledError`（上报取消后 re-raise）、`Exception`（上报错误）→ finally 恢复 ContextVar。工作目录指令（相对路径规则、上传文件清单）动态拼进用户消息。

### 5.2 subagents/ — 字典式子智能体

三个配置模块（`network_search_agent` / `database_query_agent` / `knowledge_base_agent`）结构相同：从 `prompts.yml` 取 name/description/system_prompt + 绑定各自 tools 列表。**路由依据是 description 语义**，由主智能体概率性判断。工作流约束写在各自 system_prompt（如数据库助手"先列表→预览→再写 SQL"）。

### 5.3 llm.py / prompts.py

- `llm.py`：`init_chat_model(LLM_QWEN_MAX, model_provider="openai")` 单例，全部智能体共用。
- `prompts.py`：`yaml.safe_load` 加载 `app/prompt/prompts.yml`，导出 `main_agent_content` / `sub_agents_content`。

---

## 6. 模块详解：tools 层

所有工具为 LangChain `@tool` 同步函数（签名+docstring 暴露给模型），统一模式：**monitor 埋点 → 安全校验 → 执行 → 异常转中文错误文本返回**。

### 6.1 [db_tools.py](../app/tools/db_tools.py) — SQL 三层防护的核心

| 函数 | 职责 |
|------|------|
| `assert_readonly_sql(query)` | 剥注释（`--`/`#`/`/* */`）→ 拒多语句（stacked queries）→ 白名单开头（SELECT/SHOW/DESCRIBE/EXPLAIN）→ 全句禁写关键字（INSERT/UPDATE/.../LOAD_FILE/INTO OUTFILE） |
| `validate_table_name(name)` | 表名白名单 `^[A-Za-z0-9_]{1,64}$`，拼接时反引号包裹 |
| `enforce_select_limit(sql)` | sqlglot 按 MySQL 方言解析，无 LIMIT 的 SELECT **及 UNION 等集合操作**自动补 `LIMIT 1000`（`SQL_MAX_LIMIT` 可配）；解析失败 fail-closed 拒绝 |
| `get_db_config()` | 集中读 MYSQL_* 环境变量，缺失核心项抛 ValueError |
| `list_sql_tables()` / `get_table_data(table_name)` / `execute_sql_query(query)` | 三个模型可用工具；每次调用建连、with 管理资源、异常返回中文提示 |

**纵深防御**：工具层校验（第一层）+ MySQL 只读账号 `deepsearch_ro`（第二层，docker init 脚本创建，GRANT SELECT）。

### 6.2 [tavily_tool.py](../app/tools/tavily_tool.py)

`internet_search(query, topic, max_results, include_raw_content)`：埋点只上报参数不上报结果（防事件体过大）；**模块级构造 TavilyClient**——无 `TAVILY_API_KEY` 时 import 即失败（测试靠 conftest 注入占位密钥，Gotcha #1）。

### 6.3 [ragflow_tools.py](../app/tools/ragflow_tools.py)

`get_assistant_list()`（发现助手+知识库绑定）与 `create_ask_delete(chat_name, question)`（临时会话提问后即删——防污染会话历史、不支持追问的**设计取舍**）。

### 6.4 文件工具（markdown/pdf/read）

三者共同模式：`get_session_context()` 拿目录 → `resolve_path` 收容校验 → 捕获 `PathEscapeError` 返回引导文本（不抛异常打断循环）。`read_file_content` 按后缀分发（md/txt 直读、docx 段落、pypdf、pandas head+describe），解析依赖按需 import（缺库只影响对应格式）。

---

## 7. 模块详解：utils 与 prompt

### 7.1 [path_utils.py](../app/utils/path_utils.py) — 路径安全核心

`resolve_path(filename, session_dir=None) -> str` 的安全契约：

1. 剥离模型常见虚拟前缀（`/workspace` `/mnt/data` `/home/user`）；
2. `updated/` 历史路径折叠为文件名（上传文件任务启动时已复制进 session 目录）；
3. **绝对路径一律拒绝**（含 Windows 盘符），要求相对路径；
4. resolve 后收容校验：结果必须 `is_relative_to(session_path)`，越界抛 `PathEscapeError`（含引导模型自我纠正的中文消息）。

不枚举恶意输入，只验证结果落在合法范围内——收容（containment）而非黑名单。

### 7.2 word_converter.py

MD→PDF 实际转换（reportlab），被 `pdf_tools` 调用；工具层只管路径解析。

---

## 8. 前端架构（React + Vite + AntD）

```
frontend/src/
├── App.tsx                      # 布局与轮次(turns)状态编排
├── hooks/useDeepAgentSession.ts # ★ 核心会话状态机
├── lib/
│   ├── api.ts                   # HTTP 封装（自动注入 X-API-Key）+ 令牌 + blob 下载
│   ├── config.ts                # API/WS 地址推导 + VITE_API_KEY
│   └── thread.ts                # thread_id 生成与 localStorage 持久化
├── components/                  # ConversationThread / EventStream / FileDock /
│                                # MissionComposer / AgentTopology / StatusStrip ...
└── types.ts                     # MonitorMessage(含 seq/replay) / OutputFile / ...
```

### 8.1 useDeepAgentSession.ts（前端最重要的文件）

**WS 连接状态机**（`connect` 为 async）：

1. 若配置 API_KEY：先 `POST /api/token`（头认证）换 60s 令牌；失败回退 `api_key` 查询参数（兼容保底）；
2. URL 附 `last_seq`（已收最大事件序号，供服务端差量补发）；
3. **onmessage 的 seq 处理协议**：
   - `replay` 事件：直接接受并推进 `lastSeqRef`（补发批次自身可能因服务端裁剪跳号，不参与跳号检测）；
   - 实时事件：resync pending 期间丢弃（防一次突发烧穿补发预算）；`seq <= lastSeq` 丢弃（补发/实时重叠去重）；**跳号且预算（3 次）未用尽 → `socket.close()` 触发立即重连补发**；
4. **onclose 重连**：resync pending → 立即重连（`setTimeout 0`）；常规断线 → **指数退避 + 抖动**（2s→4s→…上限 60s，连接成功清零）；
5. `submitTask` 时重置 `lastSeqRef`（新任务新事件流，防服务端重启后 seq 重置被误判重复）。

### 8.2 下载（P1-2 后）

`downloadSessionFile(threadId, path)`：fetch + `X-API-Key` 头 + blob → 临时 `<a>` 触发另存。**任何形式的凭据都不出现在 URL**。FileDock / ConversationThread 两处下载按钮均走此函数，失败经 antd message 提示。

---

## 9. 评测体系 eval

```
eval/
├── cases.py   # 20 个用例：sql 12（确定性子串判分）/ routing 2 / web 4 / multi 2（LLM-as-judge）
├── judge.py   # judge_sql（子串包含）/ judge_routing（工具命中）/ judge_with_llm（1-5 分）
└── runner.py  # 直接调 run_deep_agent（不启 HTTP），monkeypatch monitor.report_* 捕获结果
```

运行：`python -m eval.runner [--category sql|--case id|--runs 3|--out report.json]`。每用例独立 `eval-{id}` thread + `user_id=eval`，产物落 `output/user_eval/` 便于人工核查。**需真实 .env**（LLM/Tavily/MySQL/RAGFlow）。ground truth 依 `docker/mysql/mysql.sql` 手工核算——**改库数据必须复核**。

---

## 10. 基础设施与配置

### 10.1 docker/

`docker-compose.yaml`：MySQL 8.4，端口 `MYSQL_PORT`（默认 3306，.env.example 建议 3307 防冲突），utf8mb4 + TRADITIONAL + 时区 +08:00；initdb 挂载两个脚本——`01-init-pharma.sql`（教学数据）与 `02-create-readonly-user.sh`（创建 `deepsearch_ro` 只读账号，SQL 纵深防御第二层）。

### 10.2 环境变量全表（.env.example）

| 组 | 变量 | 默认 | 说明 |
|----|------|------|------|
| LLM | `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `LLM_QWEN_MAX` | — | DashScope 兼容接口 |
| 搜索 | `TAVILY_API_KEY` | — | 无则 tavily_tool import 失败 |
| RAG | `RAGFLOW_API_URL` / `RAGFLOW_API_KEY` | — | 知识库子智能体 |
| MySQL | `MYSQL_HOST/PORT/USER/PASSWORD/DATABASE/CHARSET/...` | — | 建议用只读账号 |
| 认证 | `API_KEYS` / `ALLOW_DEV_MODE` / `LINK_TOKEN_TTL_SECONDS` | 空/关/60 | fail-closed；未配置且未开 dev → 503 |
| CORS | `CORS_ORIGINS` | 本地 5173 | 逗号分隔白名单 |
| 上传 | `MAX_UPLOAD_SIZE_MB` / `MAX_UPLOAD_TOTAL_MB` / `MAX_UPLOAD_FILES` | 20/100/20 | 单文件/总量/数量 |
| 限流 | `RATE_LIMIT_TASK` / `RATE_LIMIT_UPLOAD` / `RATE_LIMIT_TOKEN` | 10/30/30 每分钟 | limits 库语法 |
| 成本 | `MODEL_RUN_LIMIT` / `MODEL_THREAD_LIMIT` / `MODEL_TOKEN_RUN_LIMIT` / `TASK_TIMEOUT_SECONDS` | 80/300/150万/600 | 调用次数 + token 预算 + 硬超时 |
| 持久化 | `CHECKPOINT_DB` / `EVENT_DB` / `EVENT_MAX_PER_STREAM` / `EVENT_REPLAY_LIMIT` | app/data/... /1000/100 | 会话状态 + 事件回放 |

---

## 11. 依赖关系总览

### 11.1 模块间依赖（箭头 = import）

```
server.py ──→ auth.py / monitor.py / event_store.py / main_agent.run_deep_agent
monitor.py ─→ event_store.py / context.py
main_agent.py ─→ llm.py / prompts.py / subagents/* / tools(文件三件套) / monitor / context
subagents/network_search_agent ─→ tools/tavily_tool
subagents/database_query_agent ─→ tools/db_tools
subagents/knowledge_base_agent ─→ tools/ragflow_tools
tools/markdown_tools|pdf_tools|upload_file_read_tool ─→ utils/path_utils / context / monitor
```

**依赖方向约束**：tools 不 import api 的路由层（只经 monitor/context 弱耦合）；prompt 只在 yml；测试不 import 真实运行时目录。

### 11.2 已知的模块级初始化陷阱（Gotchas）

1. `tavily_tool.py` 模块级构造 `TavilyClient`：缺 `TAVILY_API_KEY` 时 **import 即崩**——直接 `python -c "from app.api.server import app"` 会失败，测试靠 conftest 注入占位密钥；
2. `ragflow_tools.py` 模块级构造 RAGFlow 客户端（同理）；
3. `prompts.py` 模块级 load YAML 并 print（调试遗留）；
4. server.py 常量在**导入时**读 env——测试必须在 import 前注入（conftest 保证）。

### 11.3 关键第三方依赖

`deepagents==0.5.7`（固定）、`langchain==1.2.17` / `langgraph==1.1.10`（框架核心，版本敏感）、`slowapi`（限流）、`sqlglot`（SQL 改写）、`aiosqlite`（事件库）、`langgraph-checkpoint-sqlite`（checkpointer）。dev 组：`pytest` + `httpx`（TestClient）。

---

## 12. 运行方式

### 12.1 后端（deepsearch-agents/ 下）

```bash
python -m uv sync --group dev          # 安装依赖（本机无全局 uv，用 python -m uv）
python -m uv run uvicorn app.api.server:app --port 8000 --reload   # 启动（需 .env）
```

前置：复制 `.env.example` 为 `.env` 并填 `OPENAI_API_KEY / TAVILY_API_KEY / MYSQL_*`。**认证 fail-closed**：未配置 `API_KEYS` 且未设 `ALLOW_DEV_MODE=1` 时业务接口返回 503（本地联调设 `ALLOW_DEV_MODE=1`）。

### 12.2 数据库

```bash
cd docker && docker compose up -d      # MySQL 8.4 + 教学数据 + 只读账号
```

### 12.3 前端（frontend/ 下）

```bash
pnpm install
pnpm dev                               # Vite :5173
pnpm exec tsc -b                       # 类型检查
```

前端经 `VITE_API_KEY`（.env.local）注入密钥；后端未配置密钥（开发模式）时留空即可。

### 12.4 测试与评测

```bash
python -m uv run pytest tests/ -q      # 154 用例，无需真实 LLM/MySQL，约 7 秒
python -m eval.runner                  # 评测（需真实服务与密钥）
```

---

## 13. 测试体系

**154 个用例，全部无需真实 LLM/MySQL**（纯函数 + TestClient，`run_deep_agent` monkeypatch 为空协程）：

| 文件 | 数 | 覆盖 |
|------|----|------|
| test_path_safety.py | 14 | 穿越/盘符/`/etc/passwd`/updated 旁路全部拒绝；正常相对路径放行 |
| test_sql_guard.py | 45 | 只读白名单/注释伪装/多语句/load_file/表名注入/自动 LIMIT/UNION 补 LIMIT |
| test_api_security.py | 31 | 上传四重校验/回滚/下载越权/CORS/状态码 |
| test_auth.py | 35 | 密钥解析/401/500/fail-closed/租户隔离/非 ASCII/短时令牌（TestLinkTokens 7） |
| test_rate_limit.py | 7 | 限流键/429/独立配额/认证先于限流 |
| test_checkpointer.py | 8 | 持久化回环（新实例=重启）/线程隔离/惰性初始化/中间件透传/任务超时/token 预算 |
| test_event_replay.py | 14 | seq 递增/差量/流隔离/裁剪/重启可读/并发有序/WS 补发协议/租户回放隔离 |

**测试基建约定**（改代码必读）：

- `conftest.py` 在 import 前注入占位密钥（`OPENAI_API_KEY` 等）+ `ALLOW_DEV_MODE=1` + 高限流上限 + `EVENT_DB` 指向系统临时目录；
- API 类测试必须用 autouse fixture 把 `output_dir`/`updated_dir` 指向 `tmp_path`（**禁止写真实运行时目录**，审查修复 R-4 的教训）；
- 改任何校验逻辑必须同步加测试。

---

## 14. 当前能力边界与演进路线

### 14.1 已具备（单机可上线口径）

认证（fail-closed）+ 多租户复合键隔离 + 路径收容 + SQL 三层防护 + 上传四重校验 + 限流 + 模型调用/token/超时三重成本护栏 + 会话状态持久化（checkpointer）+ 事件回放（seq + last_seq 差量补发）+ 短时令牌（密钥不进 URL）+ 154 回归测试 + 20 条评测集最小版。

### 14.2 已知边界（如实）

| 项 | 状态 |
|----|------|
| **任务在进程内**（唯一 P0） | `asyncio.create_task` 重启即丢、无法多副本；方案见 TASK_QUEUE_DESIGN.md（ARQ + Redis + Postgres） |
| 事件/令牌存储进程内 | 事件库接口已对齐 Redis Stream 语义（append/read_after ↔ XADD/XREAD），令牌登记表同批外置 |
| 工具无网络重试 | Tavily/RAGFlow 失败直接把异常文本交给模型 |
| 同步工具疑似阻塞 | mysql-connector/Tavily 均同步 @tool，LangChain 理论上丢线程池，未压测验证 |
| 评测集 20 条 | 最小版已建，待扩 50 条接 CI |
| 无结构化日志/指标 | 全部 print（P2） |

### 14.3 提交脉络（production-hardening，`git diff main` 为准）

| 提交 | 内容 |
|------|------|
| `70f3162` | 第一批：安全加固（路径/SQL/上传/越权/CORS） |
| `d2bc74e` | 第二批：API Key 认证 + 多租户 + SQLite checkpointer |
| `62f13ab` | 第三批：限流 + fail-closed + 模型调用上限 + 审查修复 4 项 |
| `3ccd40b` | UNION 等集合操作补 LIMIT |
| `9f04b45` | 评测集最小版 + P0-2/P0-3 设计方案 |
| `6b4a5dc` | 第四批：事件回放 + 任务硬超时 + 审查修复 3 项 |
| `f741fb6` | 单任务 token 预算熔断 |
| `2a0db27` | 短时链接令牌（P1-2） |

---

*本文档与代码同步维护：结构性改动（新模块/接口变更/常量调整）后需更新对应章节；数字口径以 `git diff main` 与 pytest 实测为准。*
