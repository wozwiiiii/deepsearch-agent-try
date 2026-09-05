# deepsearch-agents Code Wiki

> **生成基准（2026-09-05 更新）**：`main` 分支，HEAD `4aebb83`，154 个测试全绿（实测 28.80 秒）。
>
> **⚠️ 分支表述更正**：本文件此前版本写"生成基准 `production-hardening` 分支（2026-08-30，HEAD `2a0db27`）"——**该分支不存在**（`git branch -a` 仅 `main`）。全部增量均在 `main` 上，**查看增量请用 `git diff df6d52e..HEAD`**（`df6d52e` 为上游 didilili 最后一笔，2026-05-18），`git diff main` 恒为空。
>
> 规模实测：后端 `app/` 28 个 `.py` / 3,351 行；`tests/` 154 用例；`eval/` 45 条；`frontend/src/` 19 文件 / 2,140 行。
> 本文档面向需要快速理解代码结构的开发者与评审者。改造动机的"原问题→修复→验证"明细见 [PRODUCTION_NOTES.md](PRODUCTION_NOTES.md)；本文聚焦**现状结构 + 设计决策的为什么**，所有代码片段摘自当前分支真实文件。

---

## 目录

1. [项目概览](#1-项目概览)
2. [整体架构](#2-整体架构)
3. [技术亮点与架构设计决策（面试重点）](#3-技术亮点与架构设计决策面试重点)
4. [目录结构](#4-目录结构)
5. [模块详解：api 层](#5-模块详解api-层)
6. [模块详解：agent 层](#6-模块详解agent-层)
7. [模块详解：tools 层](#7-模块详解tools-层)
8. [模块详解：utils 与 prompt](#8-模块详解utils-与-prompt)
9. [前端架构](#9-前端架构react--vite--antd)
10. [评测体系 eval](#10-评测体系-eval)
11. [依赖关系总览](#11-依赖关系总览)
12. [运行方式](#12-运行方式)
13. [测试体系](#13-测试体系)
14. [当前能力边界与提交脉络](#14-当前能力边界与提交脉络)

---

## 1. 项目概览

**定位**：多智能体深度研究系统（Deep Research Agent）。用户提交研究任务，主智能体调度三个专职子智能体从四种数据源（公网 Tavily / MySQL / RAGFlow 知识库 / 上传文件）检索，产出 Markdown/PDF 报告，全过程经 WebSocket 实时推送前端。

**与上游的关系（必须如实）**：底座是开源教学项目 [didilili/deepsearch-agents](https://github.com/didilili/deepsearch-agents)（MIT，对应教程"深度研搜"实战）。

- **上游基线**：`df6d52e`（作者 didilili，2026-05-18）；此前 20 笔提交均非本人所作。
- **本人增量**：18 笔提交（`d0f6eed` → `4aebb83`），**53 文件 / +6193 −546 行**，全部在 `main` 分支上线性累积。
- **查看增量的正确命令**：`git diff df6d52e..HEAD`。⚠️ 本文件旧版写的"`git diff main` 可逐文件核对"**已失效**——增量已在 main 上，该命令输出为空。
- 核心内容：安全加固、认证/多租户、限流/成本、事件回放，以及后续增量（提示词矛盾修复、同步 I/O 超时重试、结构化日志、评测集扩量、CI 结构自检）。

**技术栈**：

| 层 | 技术 |
|----|------|
| Agent 框架 | DeepAgents 0.5.7 + LangGraph 1.1.10 + LangChain 1.2.17 |
| LLM | OpenAI 兼容协议（`init_chat_model`，实际为 DashScope qwen-max） |
| 后端 | FastAPI + uvicorn，slowapi（限流），aiosqlite（事件库/checkpointer） |
| 工具客户端 | tavily-python、mysql-connector-python、ragflow-sdk、pypdf/python-docx/openpyxl |
| 前端 | React 19 + TypeScript + Vite + Ant Design 5 + pnpm |
| 数据 | MySQL 8.4（Docker；教学数据：药品 50 / 库存 150 / 销售记录 100） |

---

## 2. 整体架构

### 2.1 分层图

```
┌──────────────────────────────────────────────────────────────────┐
│ 前端 React (Vite dev :5173)                                       │
│  App.tsx → useDeepAgentSession（WS 会话状态机）→ components/*     │
└───────┬──────────────────────────────────▲───────────────────────┘
        │ HTTP (X-API-Key 头)              │ WebSocket (token / last_seq)
┌───────▼──────────────────────────────────┴───────────────────────┐
│ api 层  server.py（接口/上传/下载/限流/WS 端点）                    │
│         auth.py（认证 + 租户 + 短时令牌）                          │
│         monitor.py（事件总线：落库→推送）                          │
│         event_store.py（SQLite 事件库，seq 差量回放）              │
│         context.py（ContextVar：session_dir/thread_id）           │
├──────────────────────────────────────────────────────────────────┤
│ agent 层 main_agent.py（组装 DeepAgent + 执行 + 超时/token 预算）  │
│           subagents/（三个字典式子智能体配置）                     │
├──────────────────────────────────────────────────────────────────┤
│ tools 层 tavily / db / ragflow / markdown / pdf / upload_read     │
├──────────────────────────────────────────────────────────────────┤
│ utils 层 path_utils（收容校验） word_converter（MD→PDF）           │
└──────────────────────────────────────────────────────────────────┘
         外部服务：LLM API │ Tavily │ MySQL │ RAGFlow
```

**分层契约**（AGENTS.md）：api 层不碰业务，tools 层不关心调用方。

### 2.2 一次任务的完整生命周期

```
1. POST /api/task (X-API-Key)
   → 认证 → 限流(10/min) → validate_thread_id → 复合键 {user_id}-{thread_id}
   → asyncio.create_task(run_deep_agent) → 立即返回 {"status":"started"}

2. run_deep_agent（后台协程，wait_for 600s 硬超时包裹）
   → 建 output/user_{uid}/session_{tid} 目录，复制 updated/ 上传文件
   → ContextVar 写入目录/路由键 → monitor.report_session_dir（事件①）
   → agent.astream() 逐片段消费：
       usage_metadata → 累计 token（超 150 万熔断）
       task 工具调用 → report_assistant（子智能体路由事件）
       最终文本 → report_task_result

3. 每条 monitor 事件：event_store.append（拿全局 seq）→ WS.send_json(payload+seq)

4. 前端：实时流 seq 跳号 → 主动断开重连（带 last_seq）
   → server 先 read_after 差量补发（replay 标记）→ 再 register 实时推送
```

### 2.3 多租户复合键（贯穿全系统的隔离根）

`{user_id}-{thread_id}` 统一用于四处：`active_tasks` 字典键、WebSocket 路由键、LangGraph checkpointer thread_id、事件流 task_key。约束：user_id 限 `^[a-z0-9]{1,15}$`（**禁连字符**），thread_id 限 `^[A-Za-z0-9_-]{1,48}$`，复合后 ≤64 位。目录隔离 `output/user_{uid}/session_{tid}`——隔离由**路径拼接的构造**保证，不靠逐处授权判断。

---

## 3. 技术亮点与架构设计决策（面试重点）

> 本章每个决策按"问题 → 备选与取舍 → 代码证据 → 验证"组织。原则：不美化、不回避——每个亮点同时给出它的**边界**。

### 3.1 Agent 自纠闭环：工具返回错误字符串，而非抛异常

**这是本项目 harness 设计的第一原则，也是与"显式校验图节点"方案的核心分野。**

上游教程的"电商问数"项目（同一作者的另一个实战）用 **LangGraph 显式节点**做 SQL 闭环：`generate_sql → validate_sql（EXPLAIN 校验）→ 条件边 → correct_sql（错误信息回传模型修正）→ run_sql`，控制流由图结构驱动。本项目走的是另一条路：**校验内嵌在工具层，控制流由 Agent 循环驱动**——

```python
# app/tools/markdown_tools.py（generate_markdown 内，文件工具同模式）
try:
    full_path_str = resolve_path(full_input_path, session_dir)
except PathEscapeError as e:
    return str(e)   # ← 不抛异常，把引导性错误文本作为工具结果还给模型
```

`PathEscapeError` 的消息本身就在引导模型自我纠正：

```python
# app/utils/path_utils.py
super().__init__(
    f"路径被拒绝: {filename}（{reason}）。"
    f"请只使用当前工作目录内的相对路径，例如 'report.md'。"
)
```

**为什么这样选**：DeepAgents 的子智能体任务是开放式研究（查网、查库、查文档、写报告的组合由模型规划），没有固定管线可言；如果把每次工具调用都拆成 validate/correct 图节点，图会爆炸。而"错误文本→模型重试"天然复用了 Agent 的工具循环——模型收到 `路径被拒绝…请使用相对路径` 后下一轮就会改写参数。

**两种模式的客观对比**（面试可讲）：

| | 显式图节点（电商问数式） | 工具层校验（本项目） |
|---|---|---|
| 控制流 | 图结构决定，确定性 | 模型决定重试，概率性 |
| 可测性 | 节点单测 + 条件边断言 | 校验函数单测 + 端到端评测 |
| 适用 | 固定管线（NL2SQL 就是四步） | 开放任务（研究型 Agent） |
| 代价 | 灵活性差 | 可能多烧一轮模型调用 |

**边界（如实）**：模型收到错误文本后**可能**不纠正而放弃——概率性行为，靠评测集（`eval/` routing 用例断言工具命中）兜底，不靠假设。

**验证**：`test_path_safety.py` 14 个用例（穿越/盘符/`/etc/passwd`/updated 旁路全部返回拒绝文本而非异常）。

### 3.2 SQL 安全：三层纵深 + fail-closed 改写

**问题（第一性）**：模型生成的 SQL 是不可信输入。原版直接 `cursor.execute(模型生成的SQL)` 且 `autocommit=True`——模型生成 `DROP TABLE` 会真实执行。

三层防御，每层假设上一层会被绕过：

**第一层（工具层正则白名单）**——`assert_readonly_sql`，先剥注释防伪装再校验：

```python
# app/tools/db_tools.py
_SQL_COMMENT_PATTERN = re.compile(r"--[^\n]*|#[^\n]*|/\*.*?\*/", re.S)   # 剥注释防伪装
_READONLY_SQL_START_PATTERN = re.compile(r"^\s*(SELECT|SHOW|DESCRIBE|DESC|EXPLAIN)\b", re.I)
_FORBIDDEN_SQL_KEYWORD_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|CREATE|TRUNCATE|RENAME|"
    r"GRANT|REVOKE|LOCK|UNLOCK|CALL|SET|KILL|SHUTDOWN|HANDLER|LOAD|"
    r"LOAD_FILE|LOAD_DATA|PREPARE|EXECUTE"
    r")\b|INTO\s+(OUTFILE|DUMPFILE)", re.I)

def assert_readonly_sql(query: str) -> str:
    cleaned = _SQL_COMMENT_PATTERN.sub(" ", query).strip().rstrip(";").strip()
    if ";" in cleaned:                          # 拒多语句（stacked queries）
        raise SQLSafetyError("禁止一次执行多条 SQL 语句")
    if not _READONLY_SQL_START_PATTERN.match(cleaned): ...
    if _FORBIDDEN_SQL_KEYWORD_PATTERN.search(cleaned): ...
```

**第二层（SQL 改写）**——`enforce_select_limit` 用 sqlglot 按 MySQL 方言解析，无 LIMIT 的 SELECT 自动补 `LIMIT 1000`。这里有一次真实的审查修复：原实现只判 `isinstance(expression, Select)`，**UNION/INTERSECT/EXCEPT（sqlglot 的 SetOperation）会漏过 LIMIT 注入**，已扩展：

```python
# app/tools/db_tools.py（审查修复：集合操作也补 LIMIT）
if (
    isinstance(expression, (sqlglot.exp.Select, sqlglot.exp.SetOperation))
    and expression.args.get("limit") is None
):
    expression = expression.limit(limit)
```

且改写本身是 **fail-closed** 的：sqlglot 解析失败不执行、抛 `SQLSafetyError` 让模型改写重试（回到 3.1 的自纠闭环），而不是"解析不了就放行原句"。

**第三层（账号层）**：docker initdb 脚本创建仅有 `GRANT SELECT` 的 `deepsearch_ro` 账号，`.env` 用它连接——工具层被绕过时数据库权限兜底。

**面试金句**：纵深防御 = 假设每层都会失效；正则防的是"大概率攻击"，账号权限防的是"正则被绕过"。

**验证**：`test_sql_guard.py` 45 个用例（注释伪装、多语句注入、`load_file` 读敏感文件、反引号逃逸、UNION 补 LIMIT）。

### 3.3 路径收容校验：containment，而非黑名单

**问题**：模型可能被提示注入诱导读 `/etc/passwd` 或 `.env`（里面有 API Key）。枚举恶意输入是打地鼠；本项目的做法是**只验证结果落在合法范围内**：

```python
# app/utils/path_utils.py::resolve_path（核心四步）
for prefix in ALLOWED_VIRTUAL_PREFIXES:        # 1. 剥离 /workspace 等模型幻觉前缀
    if path_str.startswith(prefix + "/"): ...
if "updated/" in path_str:                     # 2. 历史旁路折叠为文件名
    path_str = path_str.split("updated/")[-1].split("/")[-1]
if Path(path_str).is_absolute():               # 3. 绝对路径一律拒绝（含 Windows 盘符）
    raise PathEscapeError(raw, "禁止使用绝对路径")
resolved = (session_path / path_str).resolve()
if not resolved.is_relative_to(session_path):  # 4. 收容校验：resolve 展开后必须在会话目录内
    raise PathEscapeError(raw, "路径越出会话目录")
```

关键点：**先 `resolve()` 再校验**——`../` 等间接形式会被 resolve 展开，展开后越界即拒绝。不枚举恶意 pattern，恶意形态无穷、合法范围只有一个。

**边界（如实）**：`rglob` 列文件时 Python <3.13 默认跟随目录符号链接——当前系统无入口可创建 symlink 故不可利用，已记录待办（引入解压/共享存储前需加 `is_symlink()` 过滤）。

### 3.4 多租户复合键：一处定义、四处统一、构造保证

**为什么是 `{user_id}-{thread_id}` 而不是两个独立字段**：教学原版所有状态只按 `thread_id` 区分——两个用户用同一个 thread_id 时，A 能取消 B 的任务、monitor 事件串台。修复不是逐处加 if，而是**让隔离成为构造属性**：

```python
# app/api/server.py
def composite_task_key(user_id: str, thread_id: str) -> str:
    """active_tasks、WebSocket 路由和 LangGraph thread_id 共用同一形式
    "{user_id}-{thread_id}"，确保 A 用户无法取消/接收 B 用户的任务"""
    return f"{user_id}-{thread_id}"

def user_scope_dir(base_dir: Path, user_id: str, thread_id: str) -> Path:
    return base_dir / f"user_{user_id}" / f"session_{thread_id}"   # 目录由服务端拼接
```

**字符集约束的必要性（面试常被追问）**：user_id 若允许连字符，`alice-b` + 线程 `c` 与 `alice` + 线程 `b-c` 拼出同一个键，租户边界被字符集撞破——所以 user_id 限 `^[a-z0-9]{1,15}$`，按第一个连字符切分永远无歧义。

文件接口**只接受 thread_id**（`/api/files`、`/api/download` 不收绝对路径），目录由服务端拼接——客户端即使传入 `../../` 也只会定位到自己目录内。

**验证**：`test_auth.py::TestTenantIsolation`（同 thread_id 跨租户 404）、`test_event_replay.py`（跨租户事件回放隔离——B 租户连相同 thread_id 第一条收到的是 pong 而非 A 的回放）。

### 3.5 事件回放协议：seq / last_seq / replay 标记，语义对齐 Redis Stream

**问题**：monitor 事件原来直接 `WS.send_json()`，发完即丢——断线期间的事件（含**最终答案**）永久丢失；事件无序号，前端无法发现丢件。

设计成一套与 Redis Stream 语义一一对应的协议（这是**可替换性设计**的实例）：

| 本项目 | Redis Stream 等价 |
|---|---|
| `append()` 返回全局自增 seq（AUTOINCREMENT） | `XADD` 的自增 ID |
| `read_after(last_seq)` 返回其后全部差量 | `XREAD` 从某 ID 之后读 |
| 写入时按 `max_per_stream` 裁剪 | `XADD MAXLEN ~ N` |

**关键顺序约束——先落库拿 seq，再推送**：

```python
# app/api/monitor.py::_persist_and_send
try:
    seq = await event_store.append(thread_id, payload["event"], payload["message"], payload["data"])
    payload["seq"] = seq          # seq 必须在推送前写入 payload，前端才能检测丢件
except Exception as e:
    print(f"[Monitor] 事件持久化失败（该事件将不可回放）: {e}")   # 降级不阻塞实时推送
await self.websocket_manager.send_to_thread(payload, thread_id)
```

**补发与实时的串行化——先补发、后注册**（顺序错了补发期间的新事件会与历史事件交错下发）：

```python
# app/api/server.py::websocket_endpoint
replayed = await event_store.read_after(routing_key, last_seq)   # 差量补发
for event_payload in replayed:
    await websocket.send_json(event_payload)
manager.register(websocket, routing_key)   # 补发完成后才注册实时推送
```

**前端的丢件检测协议**（`useDeepAgentSession.ts`）三规则，每条对应一个真实攻击面：

```typescript
if (payload.replay) {                       // 规则1：补发事件不参与跳号检测
  lastSeqRef.current = Math.max(lastSeq ?? 0, payload.seq);
  // —— 否则服务端裁剪后补发批次自身跳号 → 无限补发循环
} else if (lastSeq !== undefined) {
  if (payload.seq <= lastSeq) return;       // 规则2：补发/实时重叠窗口去重
  if (payload.seq > lastSeq + 1 && resyncAttemptsRef.current < MAX_RESYNC_ATTEMPTS) {
    resyncAttemptsRef.current += 1;         // 规则3：跳号=丢件，主动断开触发补发
    socket.close(); return;                 // 预算 3 次防死循环
  }
}
```

**两次审查抓到的真实缺陷**（面试讲"测试在防回归"的素材）：
- R-1：跳号触发 `close()` 后、`onclose` 生效前，同一突发的每条实时事件各耗一次补发预算——3 条突发即烧穿上限。修复：resync pending 期间丢弃实时事件（重连补发会按序重投）。
- R-2：显式 `last_seq` 的差量误用首次连接的 100 条上限——断线积压 >100 时一轮补不完，多轮烧穿预算。修复：差量上限放宽到 `max_per_stream`（裁剪天然边界），语义对齐 XREAD 读尽积压。

**为什么是 SQLite 而非设计稿的 Redis Stream**（第一性原理）：当时是单进程部署，引入 Redis 只为存事件不划算——先修真实缺陷（断线丢最终答案），不提前引入用不上的基础设施；接口已对齐 XADD/XREAD 语义，P0-2 多副本化时只换存储实现类。

**验证**：`test_event_replay.py` 14 用例（含 20 路并发 append 的 seq 唯一有序守卫、新 store 实例读回等价重启）。

### 3.6 认证演进：fail-closed + 按泄漏面分治的令牌方案

**fail-closed（第三批的认知迭代）**：原"未配置 API_KEYS 即开发模式"是 fail-open——部署忘配密钥就裸奔。现在：

```python
# app/api/auth.py::authenticate_api_key
keys = load_api_keys()
if not keys:
    if not is_dev_mode_enabled():        # 仅 ALLOW_DEV_MODE=1 显式放行
        raise HTTPException(status_code=503, detail="服务未配置 API_KEYS，且未设置 ALLOW_DEV_MODE=1；...")
    return Principal(user_id=DEV_USER_ID)
```

**"看似开启"的值一律拒绝**：`ALLOW_DEV_MODE=true` → 503（只有字面 `"1"` 放行），有测试用例锁定。

**常数时间比较 + 非 ASCII 防御**：

```python
if api_key.isascii():                    # 非 ASCII 密钥直接按无效处理（审查修复：
    for candidate, user_id in keys.items():   # compare_digest 遇非 ASCII 抛 TypeError → 500）
        if secrets.compare_digest(candidate, api_key):   # 时序侧信道防御
            return Principal(user_id=user_id)
```

**短时令牌（P1-2）按泄漏面分两条路径**——这是"同一个问题两种解法"的好素材：

| 路径 | 约束 | 方案 | 泄漏面 |
|---|---|---|---|
| 下载 | fetch **能**带请求头 | `fetch` + `X-API-Key` + blob 下载 | **归零**：任何凭据不进 URL |
| WS 握手 | WebSocket API **不能**带请求头 | `POST /api/token`（头认证）换 60s 令牌 | 日志里最多留一个一分钟内失效的随机值 |

```python
# app/api/auth.py::issue_link_token —— 两个刻意的设计取舍
ttl = int(os.getenv("LINK_TOKEN_TTL_SECONDS", "60"))
# ① TTL 制而非一次性：下载可能因网络失败重试，一次作废会把可恢复失败变成用户可见错误
# ② 登记表进程内 dict：多副本时随 P0-2 外置 Redis，读写语义不变
token = secrets.token_urlsafe(32)
```

`api_key` 查询参数保留为**兼容期旧入口**（前端已停止发送）——增量迁移不断旧客户端，计划随 P0-2 移除，而非一刀切。

**验证**：`test_auth.py` 35 用例（含 `TestLinkTokens` 7 个：TTL=0 注入测过期、bob 的有效令牌也只定位到 bob 的目录）。

### 3.7 成本三重护栏：三个维度各堵一个真实缺口

三道护栏不是堆砌，每一道堵前一道堵不住的缺口：

| 护栏 | 堵的缺口 | 位置 |
|---|---|---|
| ① slowapi 限流（10 任务/分钟/密钥） | 合法密钥无限发任务烧 LLM/Tavily 费用 | `server.py` |
| ② `ModelCallLimitMiddleware(run=80, thread=300)` | 单任务无限次模型调用（提示注入/规划失控空转） | `main_agent.py` |
| ③ token 预算 `MODEL_TOKEN_RUN_LIMIT=150 万` | **②不约束单次上下文长度**——80×32K≈256 万 token 无上限 | `_consume_agent_stream` |

③的实现（流式循环内累计，超限即熔断）：

```python
# app/agent/main_agent.py::_consume_agent_stream
for msg in messages:
    usage = getattr(msg, "usage_metadata", None)
    if usage:
        tokens_used += usage.get("total_tokens") or (
            usage.get("input_tokens", 0) + usage.get("output_tokens", 0))  # 兜底求和
...
if tokens_used > MODEL_TOKEN_RUN_LIMIT:   # 检查在片段处理之前，超限后不多消费一轮
    raise RuntimeError(f"单次任务 token 消耗 {tokens_used} 已超过预算上限 ...")
```

异常复用现有通道（`run_deep_agent` 的 `except Exception` → monitor 上报 → 事件落库断线可回放），零新增错误路径。

**边界（如实）**：只统计流中可见的模型消息——子智能体内部调用若未随片段上报则不计入；供应商不回传 usage_metadata 时静默失效；会话级（跨任务累计）预算需读 checkpoint 历史，未做。**没精确统计过真实单任务成本**——机制能截住失控消耗，但不是计量系统。

配套的**硬超时**同理：`asyncio.wait_for` 包住整个执行含 Agent 惰性初始化（异步生成器不能直接套 wait_for，所以先抽成 `_consume_agent_stream` 协程——这是重构的动机，不是为抽而抽）。

**验证**：`test_checkpointer.py::TestTokenBudget`（2）+ `::TestTaskTimeout`（2）。

### 3.8 上下文工程：模型只在给定约束范围内生成

与教程"SQL 生成前先准备 table_infos/metric_infos"同一思想，本项目的落点：

1. **工具返回摘要化**：`get_table_data` 限 100 行 CSV、`execute_sql_query` 自动 `LIMIT 1000`、RAGFlow 临时会话用完即删（`create_ask_delete`）、Tavily 埋点只上报参数不上报结果正文——都是防大结果集挤爆 32K 窗口；
2. **运行时动态指令**：`run_deep_agent` 把工作目录规则、上传文件清单拼进用户消息（`path_instruction`），约束模型只在当前会话目录读写、优先读附件；
3. **提示词外置**：`app/prompt/prompts.yml` + `prompts.py` 的 `yaml.safe_load`（safe_load 防对象构造风险），主/子智能体配置同源；
4. **ContextVar 隐式上下文**：`context.py` 让深层工具免层层传参拿到 session_dir/thread_id——本质是教程里 `state` 流转的"协程级"版本，代价是隐式依赖（工具需容忍 None，新人不易发现这层状态）。

**已知问题（如实，面试可主动讲）**：主智能体 system_prompt 存在自相矛盾（"只能交给文件生成助手"——该子智能体不存在；又说"你可以生成 Markdown/PDF"），模型靠容错在跑——说明提示词缺版本化管理与评测回归（评测集最小版已建，正是为此）。

### 3.9 一个可替换性设计的完整案例：checkpointer

选型被框架抽象隔离的回报：

```python
# main_agent.py::_get_agent —— 惰性初始化 + 双重检查
async with _agent_init_lock:
    if _main_agent is not None:
        return _main_agent
    saver_ctx = AsyncSqliteSaver.from_conn_string(str(db_path))
    _checkpoint_saver = await saver_ctx.__aenter__()
    _main_agent = create_deep_agent(..., checkpointer=_checkpoint_saver, ...)
```

- **为什么惰性**：AsyncSqliteSaver 持长连接，模块导入（如测试收集）就初始化会抢数据库文件；首任务时建、`asyncio.Lock`+双重检查保证并发只建一次、之后缓存复用；
- **为什么 SQLite**：单机零运维；**边界**：多副本并发写同一文件库级锁冲突，届时换 `langgraph-checkpoint-postgres` **接口不变只换 saver 实现**——与 3.5 的事件库同一条设计原则：单机够用时不引入运维成本，但把"将来要换"编码进接口边界。

**验证**：`test_checkpointer.py` 回环测试——新 saver 实例（等价服务重启）读回旧实例写入的 checkpoint。

---

## 4. 目录结构

```
deepsearch-agents/
├── app/
│   ├── api/                    # ★ 接口层：server / auth / monitor / event_store / context
│   ├── agent/                  # ★ main_agent + llm + prompts + subagents/(三个配置)
│   ├── prompt/prompts.yml      # 全部提示词（不硬编码）
│   ├── tools/                  # ★ 六个 LangChain @tool 模块
│   ├── ragflow/                # RAGFlow 连接配置
│   └── utils/                  # path_utils（收容校验）/ word_converter
├── frontend/                   # React（见 §9）
├── tests/                      # 154 个测试（见 §13）
├── eval/                       # 评测集 cases/judge/runner（45 条，2026-09-05 自 20 条扩量）
├── docker/                     # MySQL 8.4 compose + 初始化 SQL + 只读账号脚本
├── examples/                   # DeepAgents 框架 15 个教学脚本（非运行时依赖）
├── docs/                       # PRODUCTION_NOTES / 设计稿 / WORK_STATUS / 本 Wiki
└── pyproject.toml              # uv 管理（Python 3.12 锁定）
```

运行时产物（gitignore）：`app/data/checkpoints.sqlite3`、`app/data/events.sqlite3`、`app/output/`、`app/updated/`。

---

## 5. 模块详解：api 层

### 5.1 [server.py](../app/api/server.py) — 接口层核心

| 端点 | 方法 | 鉴权 | 限流 | 职责 |
|------|------|------|------|------|
| `/health` | GET | 无 | 无 | 探活 |
| `/api/task` | POST | 头 | 10/min | 启动后台 Agent 任务（create_task 立即返回） |
| `/api/task/{thread_id}/cancel` | POST | 头 | 无 | 取消（cancel + 1s 等待 → cancelled/cancelling） |
| `/api/token` | POST | 头 | 30/min | 签发 60s 短时链接令牌 |
| `/api/upload` | POST | 头 | 30/min | 多文件上传（四重校验 + 失败回滚） |
| `/api/files` | GET | 头 | 无 | 列会话产物（只收 thread_id） |
| `/api/download` | GET | 头>token>api_key | 无 | 下载（收容校验双保险） |
| `/ws/{thread_id}` | WS | token>api_key | 无 | 实时推送 + last_seq 差量回放 |

**关键函数**：`composite_task_key`（§3.4）、`validate_thread_id`（白名单 `^[A-Za-z0-9_-]{1,48}$`）、`user_scope_dir`（租户目录拼接）、`_rate_limit_key`（密钥 SHA-256 截断 16 位入键，明文不入限流状态与日志；无密钥回退 IP）、`require_principal_for_link`（直链鉴权优先级链）、`sanitize_filename`（只留 basename，剥所有目录部分）、`_rollback_saved`（多文件上传中途失败清理已写入文件——审查修复，防"部分上传"被后续任务当有效输入）。

**上传四重校验**：扩展名白名单（与读取工具支持格式一致）→ 单文件 20MB → 请求总量 100MB（单文件限制可被"一次传很多"绕过，审查修复）→ 文件数 20；aiofiles 分块写盘超限即中止并清理半成品。

**WS 端点顺序**：鉴权 → `last_seq` 校验（非 `[0-9]{1,18}` 直接 1008 拒绝）→ accept → **先补发后注册**（§3.5）→ 心跳 pong 循环。事件库读取失败降级为纯实时模式（连接不因此断）。

### 5.2 [auth.py](../app/api/auth.py)

见 §3.6。成员速查：`Principal`（frozen dataclass）、`parse_api_keys`（格式校验：用户名白名单/密钥≥16 位/查重）、`authenticate_api_key`（fail-closed + 常数时间比较）、`issue_link_token`/`resolve_link_token`（短时令牌；无效与过期统一 401 不区分原因，防探测）。

### 5.3 [monitor.py](../app/api/monitor.py) — 事件总线

- `ToolMonitor`（单例）：业务工具只调 `report_tool / report_assistant / report_task_result / report_task_cancelled / report_error / report_session_dir`，签名稳定——**工具不感知传输方式**（WS/事件库/控制台由内部决定）；
- `_emit` → `_schedule_persist_and_send`：工具可能在 LangChain 线程池里同步执行，不在 FastAPI 主循环——同循环 `create_task`，跨线程 `run_coroutine_threadsafe`（投递顺序即执行顺序，事件不乱序）；
- `ConnectionManager`：`register` 与 accept 分离（回放协议要求）；`disconnect` 只在连接实例匹配时删除（防旧连接断开误删同键新连接）。

### 5.4 [event_store.py](../app/api/event_store.py)

见 §3.5。`SqliteEventStore` 全局单例：`append`（加锁串行化保 seq 分配顺序，INSERT+裁剪单事务单 commit）、`read_after`（差量全量/最近 N 两条路径）、`close`。参数：`EVENT_DB` / `EVENT_MAX_PER_STREAM=1000` / `EVENT_REPLAY_LIMIT=100`。WAL 模式；惰性初始化（`asyncio.Lock` 双重检查）。

### 5.5 [context.py](../app/api/context.py)

`ContextVar` 保存 `session_dir` 与 `thread_id`（复合键）；`run_deep_agent` 开始 set、finally reset。工具经 `get_session_context()` 免层层传参。边界：隐式依赖，工具需容忍 None（脚本调试场景）。

---

## 6. 模块详解：agent 层

### 6.1 [main_agent.py](../app/agent/main_agent.py)

**模块级配置**（env 可覆盖）：`CHECKPOINT_DB`、`MODEL_RUN_LIMIT=80` / `MODEL_THREAD_LIMIT=300`、`TASK_TIMEOUT_SECONDS=600`、`MODEL_TOKEN_RUN_LIMIT=1500000`。

**组装**（`_get_agent`，见 §3.9 代码）：

```python
_main_agent = create_deep_agent(
    model=model,
    system_prompt=main_agent_content["system_prompt"],
    tools=[generate_markdown, convert_md_to_pdf, read_file_content],   # 主智能体只管最终交付
    checkpointer=_checkpoint_saver,
    subagents=[database_query_agent, network_search_agent, knowledge_base_agent],
    middleware=[ModelCallLimitMiddleware(
        run_limit=MODEL_RUN_LIMIT, thread_limit=MODEL_THREAD_LIMIT,
        exit_behavior="error",    # 超限抛异常由 run_deep_agent 捕获上报，而非静默截断
    )],
)
```

**为什么一主三从**（面试）：① 上下文隔离——三个数据源的原始结果不共享主智能体的 32K 窗口，子智能体独立上下文只回传结论；② 提示词专业化——"先列表再预览再写 SQL"与"多角度检索"的工作流约束分开写互不干扰；③ 可观测——每次 `task` 工具调用是明确的调度边界。**代价（主动讲）**：路由靠主智能体读 description 语义判断，是概率性的；子智能体间协作要经主智能体中转，多一跳延迟和成本。

**执行入口** `run_deep_agent(task_query, session_id, user_id="local")`：建租户目录 → 复制上传文件 → ContextVar → 上报 session_dir → `asyncio.wait_for(_consume_agent_stream(...))` → 异常分类（`TimeoutError` 上报超时 / `CancelledError` 上报取消后 re-raise / `Exception` 上报错误）→ finally 恢复 ContextVar。

`_consume_agent_stream`（token 熔断见 §3.7）同时负责：解析 `task` 工具调用上报子智能体路由、捕获最终文本上报结果。

### 6.2 subagents/ — 字典式子智能体

三个配置模块结构相同（name/description/system_prompt 来自 `prompts.yml` + 各自 tools 列表）：

```python
# app/agent/subagents/database_query_agent.py
database_query_agent = {
    "name": sub_agents_content["db"]["name"],
    "description": sub_agents_content["db"]["description"],   # ← 路由依据：主智能体读语义分派
    "system_prompt": sub_agents_content["db"]["system_prompt"],
    "tools": [list_sql_tables, get_table_data, execute_sql_query],  # 工具列表即能力边界
}
```

网络搜索绑 `internet_search`；知识库绑 `get_assistant_list + create_ask_delete`。**tools 列表即该子智能体的能力边界**——数据库助手碰不到文件系统，文件工具只在主智能体手里。

---

## 7. 模块详解：tools 层

所有工具统一模式：**monitor 埋点 → 安全校验 → 执行 → 异常转中文错误文本返回**（§3.1）。

### 7.1 [db_tools.py](../app/tools/db_tools.py) — 见 §3.2

三个模型可用工具：`list_sql_tables`（SHOW TABLES → 表名列表）、`get_table_data`（表名白名单 + 反引号包裹 + `LIMIT 100` CSV）、`execute_sql_query`（`assert_readonly_sql` + `enforce_select_limit` 链路）。每次调用建连、`with` 管理资源。

### 7.2 [tavily_tool.py](../app/tools/tavily_tool.py)

`internet_search(query, topic, max_results, include_raw_content)`。**模块级构造 `TavilyClient`**——缺 `TAVILY_API_KEY` 时 import 即失败（Gotcha：测试靠 conftest 注入占位密钥）。埋点只上报参数不上报结果正文（防监控事件体过大）。

### 7.3 [ragflow_tools.py](../app/tools/ragflow_tools.py)

`get_assistant_list`（发现助手+知识库绑定，路由信息）与 `create_ask_delete`（临时会话提问后即删）。**用完即删是设计取舍**：防会话历史污染，代价是不支持追问——面试要能讲清。

### 7.4 文件工具（markdown / pdf / upload_read）

三者共同模式（§3.1/§3.3）：`get_session_context()` → `resolve_path` 收容 → `PathEscapeError` 返回引导文本。`read_file_content` 按后缀分发（md/txt 直读、docx 段落、pypdf、pandas head+describe），解析依赖按需 import（缺库只影响对应格式，不影响工具注册）。

---

## 8. 模块详解：utils 与 prompt

### 8.1 [path_utils.py](../app/utils/path_utils.py) — 见 §3.3

`resolve_path` 四步安全契约 + `PathEscapeError`（消息即模型纠错指引）。

### 8.2 word_converter.py

MD→PDF 实际转换（reportlab）；工具层（pdf_tools）只管路径解析，转换实现与接口分层。

### 8.3 prompts.py

`yaml.safe_load`（防 YAML 对象构造风险）加载 `app/prompt/prompts.yml`，导出 `main_agent_content` / `sub_agents_content`。已知遗留：模块级 `print(sub_agents_content)` 调试输出（P2 待清）。

---

## 9. 前端架构（React + Vite + AntD）

```
frontend/src/
├── App.tsx                      # 布局与轮次(turns)状态编排
├── hooks/useDeepAgentSession.ts # ★ 核心会话状态机（本节主角）
├── lib/api.ts                   # HTTP 封装（自动注入 X-API-Key）+ fetchLinkToken + downloadSessionFile
├── lib/config.ts / thread.ts    # 地址推导 / thread_id 生成与 localStorage
├── components/                  # ConversationThread / EventStream / FileDock / ...
└── types.ts                     # MonitorMessage（含 seq/replay 字段）
```

### 9.1 useDeepAgentSession.ts — WS 连接状态机

连接前先换令牌（P1-2，失败回退兼容入口）：

```typescript
if (API_KEY) {
  try {
    const { token } = await fetchLinkToken();   // 头认证换 60s 令牌
    params.set("token", token);
  } catch {
    params.set("api_key", API_KEY);             // 兼容保底，已弃用
  }
}
if (lastSeqRef.current !== undefined) {
  params.set("last_seq", String(lastSeqRef.current));   // 差量补发游标
}
```

seq 处理三规则见 §3.5。**重连退避**：resync pending → 立即重连（`setTimeout 0`）；常规断线 → 指数退避 + 随机抖动（2s→4s→…上限 60s，连接成功清零）——固定 2s 重连在服务抖动时是雪崩放大器。

**一个易漏细节**：`submitTask` 时重置 `lastSeqRef = undefined`——新任务新事件流；若不重置，服务端重启（seq 从小值重新自增）后新事件会被当作重复丢弃。

### 9.2 下载（P1-2 后）

`downloadSessionFile`：fetch + 头 + blob → 临时 `<a>` 另存。FileDock / ConversationThread 两处按钮均走此函数。

---

## 10. 评测体系 eval

```
eval/
├── cases.py   # 45 条用例（`66f85e9` 自 20 条扩量）：sql 22（确定性子串判分）/ routing 6 / web 10 / multi 7（LLM-as-judge）
├── judge.py   # judge_sql（子串包含）/ judge_routing（工具命中）/ judge_with_llm（1-5 分）
└── runner.py  # 直接调 run_deep_agent（不启 HTTP），monkeypatch monitor.report_* 捕获结果
```

**设计要点**：monkeypatch `monitor` 的 report 方法即可捕获结果/工具调用/路由（工具调用时查 monitor 属性，替换属性即生效——同一单例的回报）；每用例独立 `eval-{id}` thread + `user_id=eval`，产物落 `output/user_eval/` 供人工核查。

运行：`python -m eval.runner [--category sql|--case id|--runs 3|--out report.json]`。**需真实 .env**。ground truth 依 `docker/mysql/mysql.sql` 手工核算——**改库数据必须复核**，否则误报。

---

## 11. 依赖关系总览

### 11.1 模块依赖（箭头 = import）

```
server.py ──→ auth / monitor / event_store / main_agent.run_deep_agent
monitor.py ─→ event_store / context
main_agent ─→ llm / prompts / subagents/* / 文件工具三件套 / monitor / context
subagents/network  ─→ tools/tavily_tool
subagents/database ─→ tools/db_tools
subagents/ragflow  ─→ tools/ragflow_tools
文件工具 ──→ utils/path_utils / context / monitor
```

方向约束：tools 不 import api 路由层（只经 monitor/context 弱耦合）；prompt 只在 yml。

### 11.2 模块级初始化陷阱（改代码前必读）

1. `tavily_tool.py` / `ragflow_tools.py` **模块级构造客户端**：缺 key 时 import 即崩——`python -c "from app.api.server import app"` 会失败，测试靠 conftest 注入占位密钥；
2. `prompts.py` 模块级 load YAML；
3. server.py 常量**导入时**读 env——测试必须在 import 前注入（conftest 保证）。

### 11.3 关键第三方依赖

`deepagents==0.5.7`（固定）、`langchain==1.2.17` / `langgraph==1.1.10`（框架核心，版本敏感）、`slowapi`（限流）、`sqlglot`（SQL 改写）、`aiosqlite`（事件库）、`langgraph-checkpoint-sqlite`（checkpointer）。dev：`pytest` + `httpx`。

---

## 12. 运行方式

```bash
# 后端（deepsearch-agents/ 下）
python -m uv sync --group dev
python -m uv run uvicorn app.api.server:app --port 8000 --reload   # 需 .env

# 数据库（docker/ 下）
docker compose up -d        # MySQL 8.4 + 教学数据 + deepsearch_ro 只读账号

# 前端（frontend/ 下）
pnpm install && pnpm dev    # Vite :5173
pnpm exec tsc -b            # 类型检查

# 测试与评测
python -m uv run pytest tests/ -q    # 154 用例，无需真实 LLM/MySQL，约 7 秒
python -m eval.runner                # 评测（需真实服务与密钥）
```

前置：复制 `.env.example` 为 `.env` 填 `OPENAI_API_KEY / TAVILY_API_KEY / MYSQL_*`。**认证 fail-closed**：未配置 `API_KEYS` 且未设 `ALLOW_DEV_MODE=1` 时业务接口 503（本地联调设 `ALLOW_DEV_MODE=1`）。环境：Windows + Git Bash，Python 3.12（`.python-version` 锁定）。

环境变量全表见 [.env.example](../.env.example)（LLM/Tavily/RAGFlow/MySQL/认证/CORS/上传/限流/成本/持久化 共 30 余项）。

---

## 13. 测试体系

**154 用例，全部无需真实 LLM/MySQL**（纯函数 + TestClient，`run_deep_agent` monkeypatch 为空协程，约 7 秒）：

| 文件 | 数 | 覆盖 |
|------|----|------|
| test_path_safety.py | 14 | 穿越/盘符/updated 旁路拒绝；正常相对路径放行 |
| test_sql_guard.py | 45 | 只读白名单/注释伪装/多语句/load_file/表名注入/自动 LIMIT/UNION |
| test_api_security.py | 31 | 上传四重校验/回滚/下载越权/CORS/状态码 |
| test_auth.py | 35 | 认证/fail-closed/租户隔离/非 ASCII/短时令牌 7 |
| test_rate_limit.py | 7 | 限流键/429/独立配额/认证先于限流 |
| test_checkpointer.py | 8 | 持久化回环/线程隔离/惰性初始化/超时/token 预算 |
| test_event_replay.py | 14 | seq/差量/流隔离/裁剪/重启可读/并发有序/WS 协议/租户隔离 |

**测试基建三条铁律**（改代码必读）：

1. conftest 在 import 前注入占位密钥 + `ALLOW_DEV_MODE=1` + 高限流上限 + `EVENT_DB` 指向系统临时目录；
2. API 类测试必须用 autouse fixture 把 `output_dir`/`updated_dir` 指向 `tmp_path`——**禁止写真实运行时目录**（审查修复 R-4 的教训：历史测试残留文件可能被真实任务读到）；
3. 改任何校验逻辑必须同步加测试。

**测试真实抓到过的问题**（面试素材）：`load_file` 防护缺口、UNION 漏 LIMIT（125→129）、非 ASCII 密钥 500、补发预算烧穿、差量截断（129→154）——测试在防回归，不是摆设。

---

## 14. 当前能力边界与提交脉络

### 14.1 已具备（单机可上线口径）

认证（fail-closed）+ 多租户复合键隔离 + 路径收容 + SQL 三层防护 + 上传四重校验 + 限流 + 三重成本护栏（次数/token/超时）+ 会话状态持久化 + 事件回放（seq + 差量补发）+ 短时令牌（密钥不进 URL）+ 同步 I/O 超时与重试（防线程池耗尽）+ 结构化日志与 trace_id 贯穿 + 154 回归测试 + 45 条评测集。

### 14.2 已知边界（2026-09-05 如实更新）

| 项 | 状态 |
|----|------|
| **任务在进程内**（唯一剩余 P0） | `asyncio.create_task`（`server.py:348`）重启即丢、无法多副本；方案见 TASK_QUEUE_DESIGN.md（ARQ + Redis + Postgres）。**至今一行未动** |
| 事件库/令牌表进程内 | 接口已对齐 Redis Stream 语义，随 P0-2 外置 |
| **评测基线从未跑过** | 45 条已建、CI 结构自检已接，但**无 `.env`（缺 Key）与 MySQL 容器，未产出任何跑分**。这是当前最大的证据缺口 |
| MySQL 查询阶段无超时 | `connection_timeout=10` 仅约束建连；查询阶段 mysql-connector-python 无可靠支持，属驱动限制 |
| 会话级（跨任务累计）token 预算未做 | run 级已完成 |
| OTel / 指标采集未做 | 结构化日志是其前置，已就位 |
| 事件库无 TTL 清理 | 当前只有条数裁剪（每 task_key 保留最近 1000 条） |
| 后端无 Dockerfile | 全项目 `find -iname 'Dockerfile*'` 为空，无法容器化部署 |

**本轮已消除的旧边界**（旧版 14.2 曾列出，现予删除并说明）：

| 原边界 | 处理结果 |
|--------|----------|
| ~~工具无网络重试~~ | ✅ `c0b5b45`：Tavily `timeout=20`、MySQL `connection_timeout=10`、LLM `max_retries=2` |
| ~~同步工具疑似阻塞（未压测验证）~~ | ✅ **实测证伪**（`c0b5b45`）：40 个阻塞任务并发下 `/health` 仍 0.0000s 返回，`run_in_executor` 不阻塞事件循环；真问题是 `wait_for` 杀不掉线程导致线程池耗尽，已按此修复 |
| ~~全部 print，无结构化日志~~ | ✅ `9506b8a`：JsonFormatter + trace_id 贯穿；生产路径 print 清零（残留 12 处经逐行核对全在 `__main__` 演示块内） |
| ~~提示词自相矛盾~~ | ✅ `c11fd2b`：主智能体提示词与真实工具列表对齐；`b1d7774` 修"你你"笔误 |
| ~~评测集 20 条待扩 50 条接 CI~~ | ✅ `66f85e9` 扩到 45 条；`3e91737` CI 接入结构自检（**非全量评测**，不调真实 Agent） |

### 14.3 提交脉络（`main` 分支，`git diff df6d52e..HEAD` 为准）

> ⚠️ 旧版标题写"（production-hardening，`git diff main` 为准）"——该分支不存在且该命令恒为空，已更正。

| 提交 | 内容 |
|------|------|
| `df6d52e` | **上游基线**（didilili，2026-05-18），此前 20 笔均非本人所作 |
| `d0f6eed` | 修正错误的文件夹命名 |
| `70f3162` | 第一批：安全加固（路径/SQL/上传/越权/CORS） |
| `d2bc74e` | 第二批：认证 + 多租户 + SQLite checkpointer |
| `62f13ab` | 第三批：限流 + fail-closed + 调用上限 + 审查修复 4 项 |
| `3ccd40b` | UNION 等集合操作补 LIMIT |
| `9f04b45` | 评测集最小版 + P0-2/P0-3 设计方案 |
| `6b4a5dc` | 第四批：事件回放 + 硬超时 + 审查修复 3 项 |
| `f741fb6` | 单任务 token 预算熔断 |
| `2a0db27` | 短时链接令牌 |
| `64c8aa9` / `4d41d02` / `f4f1995` | CODE_WIKI 文档、README 更新、CI 处理 |
| `c11fd2b` | 提示词矛盾修复（与真实工具对齐） |
| `c0b5b45` | 同步 I/O 与 LLM 调用超时/重试，防线程池耗尽 |
| `9506b8a` | 结构化日志 + trace_id 贯穿 |
| `66f85e9` | 评测集 20 → 45 条 |
| `b1d7774` | 提示词"你你"笔误 |
| `3e91737` | CI 接入评测结构自检 + 重复 id 断言 |
| `4aebb83` | 设计文档格式化 |

**本人增量合计 18 笔，53 文件 / +6193 −546**（`git diff --shortstat df6d52e..HEAD`）。其中后 7 笔（`c11fd2b`→`4aebb83`，17 文件 / +531 −116）**已提交但尚未推送远端**。

---

## 附：与上游教程"电商问数"项目的技术对照

两个项目同出一位教程作者，恰好构成同一问题的两种解法（面试可主动对比）：

| 维度 | 电商问数（LangGraph） | 本项目（DeepAgents） |
|---|---|---|
| 任务形态 | 固定管线：召回→过滤→生成 SQL→EXPLAIN 校验→校正→执行 | 开放研究：模型自主规划检索组合与产出 |
| 错误闭环 | 显式节点 `validate_sql` 写 `state["error"]`，条件边决定 `correct_sql`/`run_sql` | 校验内嵌工具层，错误文本作为工具结果回传，模型循环内自纠 |
| 状态流转 | `DataAgentState`（TypedDict）显式传递 | ContextVar 隐式上下文 + checkpointer 持久化 |
| 控制流归属 | 图结构（确定性） | 模型（概率性，靠评测兜底） |

**一句话总结**：管线确定就把它编码进图结构（可控可测），任务开放就把约束编码进工具层与提示词（灵活）——本项目的生产化改造（收容校验、fail-closed、成本护栏）全部沿"工具层约束"这条路加固，没有改变原版的控制流归属。

---

*本文档与代码同步维护：结构性改动（新模块/接口变更/常量调整）后需更新对应章节；数字口径以 `git diff df6d52e..HEAD` 与 pytest 实测为准（**不是** `git diff main`——增量已在 main 上，该命令恒为空）。*
