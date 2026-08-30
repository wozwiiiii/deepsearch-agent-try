# deepsearch-agents 工作状态报告

> **更新时间**：2026-08-30
> **分支**：`production-hardening`（基于 `main` 分支）
> **最新提交**：`9f04b45` 评测集最小版 + P0-2/P0-3 设计方案（第四批事件回放为未提交工作区改动）

---

## 一、整体进度概览

| 阶段 | 状态 | 提交 |
|------|------|------|
| 第一批：安全加固（路径/SQL/会话隔离） | ✅ 已提交 | `d0f6eed` → `70f3162` |
| 第二批：认证 + 多租户 + SQLite 持久化 | ✅ 已提交 | `d2bc74e` |
| 第三批：限流 + fail-closed + 模型调用上限 + 审查修复 | ✅ 已提交 | `62f13ab` |
| UNION 等集合操作补 LIMIT + 测试数同步 | ✅ 已提交 | `3ccd40b` |
| 评测集最小版 + P0-2/P0-3 设计方案 | ✅ 已提交 | `9f04b45` |
| 第四批：P0-3 事件回放 + 审查修复 + P1-4 任务硬超时 | ✅ 已提交 | `6b4a5dc` |
| P1-4a：单任务 token 预算熔断 | ✅ 已完成，**未提交** | 本轮增量 |
| 面试文档（5 份） | ✅ 已同步（147 测试） | — |

---

## 二、第四批改动清单（P0-3 事件回放 + 审查修复 + P1-4 硬超时，已提交 6b4a5dc；P1-4a token 预算为本轮未提交增量）

### 2.1 改动内容

| 文件 | 变更 | 说明 |
|------|------|------|
| `app/api/event_store.py`（新增） | ~200 行 | SQLite 事件库：`append` 返回全局自增 seq（等价 XADD），`read_after` 差量读取（等价 XREAD），按 `EVENT_MAX_PER_STREAM` 裁剪（等价 MAXLEN）；与 Redis Stream 语义一一对应，P0-2 时可整体替换实现 |
| `app/api/monitor.py` | 重构 `_emit` | 事件先落库拿 seq 再推 WS；持久化失败降级为不可回放，不阻塞实时推送；`ConnectionManager.register` 与 accept 分离（补发完成后才注册实时推送，防乱序） |
| `app/api/server.py` | WS 端点 | 握手支持 `last_seq`（非法值 1008 拒绝）；先补发差量再注册；首次连接补发最近 `EVENT_REPLAY_LIMIT` 条 |
| `frontend/src/hooks/useDeepAgentSession.ts` | +退避/补发 | 指数退避 + 抖动（2s→60s 上限）；seq 跳号主动重连补发（限 3 次）；补发事件跳过跳号检测；重叠窗口按 seq 去重 |
| `frontend/src/types.ts` | 类型 | `MonitorMessage` 增加 `seq` / `replay` 字段 |
| `tests/test_event_replay.py`（新增） | 14 用例 | 存储层（seq/差量/隔离/裁剪/重启可读/并发有序）、monitor 落库、WS 补发协议、租户回放隔离 |
| `app/agent/main_agent.py` | P1-4 硬超时 + P1-4a token 预算 | 流式消费抽为 `_consume_agent_stream`，`asyncio.wait_for(TASK_TIMEOUT_SECONDS=600)` 包住整个执行（含初始化）；流内累计 `usage_metadata`，超 `MODEL_TOKEN_RUN_LIMIT`（默认 150 万）熔断终止 |
| `tests/test_checkpointer.py` | +4 用例 | 任务超时 2（挂起终止上报、初始化覆盖）+ token 预算 2（超限熔断、预算内含兜底求和） |
| `tests/conftest.py` | 环境隔离 | `EVENT_DB` 指向系统临时目录，测试不写真实 `app/data/` |
| `pyproject.toml` / `.env.example` / `.gitignore` | 配套 | 显式声明 `aiosqlite`；新增事件回放 3 个 + `TASK_TIMEOUT_SECONDS` 环境变量；忽略本地 pnpm store |

**第四批代码审查修复**（对上述增量做正式审查后）：

| # | 问题 | 修复 |
|---|------|------|
| R-1 | 前端补发预算被事件风暴烧穿（close 到 onclose 间每条事件各耗一次预算） | resync pending 期间丢弃实时事件 |
| R-2 | 显式 last_seq 差量补发被 replay_limit=100 截断，大间隙需多轮补发且烧穿预算 | 差量上限放宽到 max_per_stream |
| R-3 | 每条事件 commit 两次，多一次 WAL fsync | 合并单事务 |
| R-4 | `ConnectionManager.connect` 死代码 | 删除 |
| 证伪 | asyncio.Lock 跨循环绑定（探针实证不成立，记录不修） | — |

### 2.2 验证结果

- 后端：**147 个用例全部通过**（原 129 + 事件回放 14 + 任务超时 2 + token 预算 2，`.venv/Scripts/python.exe -m pytest tests/ -q`，约 7 秒）
- 前端：`tsc -b` 零错误（注：`frontend/node_modules` 因项目目录迁移 junction 失效，已用 `pnpm install --store-dir ./.pnpm-store-local` 重装修复）

### 2.3 已知边界（如实标注）

- 补发读取与实时注册间存在毫秒级窗口，由前端 seq 跳号检测兜底；
- WS 背压下推送顺序与 seq 顺序理论上可倒置，前端跳号检测→补发闭环自愈（协议以 seq 为准）；
- 多副本事件广播仍需 P0-2（本批只解决单进程持久化与回放）；
- 事件库无 TTL 清理，只有条数裁剪（每 task_key 保留最近 1000 条）。

---

## 三、测试分布

| 测试文件 | 用例数 | 覆盖领域 |
|----------|--------|----------|
| `tests/test_path_safety.py` | 14 | 路径穿越防御 |
| `tests/test_sql_guard.py` | 45 | SQL 只读 + 自动 LIMIT + UNION |
| `tests/test_api_security.py` | 31 | 上传安全 + 会话隔离 + 回滚 |
| `tests/test_auth.py` | 28 | API Key 认证 + fail-closed + 非 ASCII |
| `tests/test_rate_limit.py` | 7 | slowapi 限流 |
| `tests/test_checkpointer.py` | 8 | SQLite 持久化 + middleware 透传 + 任务硬超时 + token 预算 |
| `tests/test_event_replay.py` | 14 | 事件回放（存储/落库/补发协议/租户隔离/并发有序） |
| **合计** | **147** | — |

---

## 四、尚未完成的待办项

| 优先级 | 任务 | 状态 |
|--------|------|------|
| **P0** | Git 提交 P1-4a token 预算熔断 | 待提交 |
| **P0** | P0-2 任务出进程（ARQ + Redis + Postgres，设计方案已写好） | 未实现 |
| **P1** | 短时一次性令牌替代查询参数密钥（P1-2） | 未实现 |
| **P1** | 工具网络重试（tenacity）；token 会话级累计预算（run 级已完成） | 未实现 |
| **P2** | 评测集扩到 50 条接 CI；可观测性（结构化日志/OTel）；事件库 TTL 清理 | 长期 |

---

## 五、企业级差距路线图（长期）

| # | 差距 | 阻塞性 | 备注 |
|---|------|--------|------|
| 1 | 任务队列与并发治理（P0-2） | 🔴 上线必须 | `asyncio.create_task` → ARQ；事件存储随本批换 Redis Stream |
| 2 | 可观测性（结构化日志 + OTel + Prometheus） | 🟡 重要 | 替换 `print`，接入 LangSmith/LangFuse |
| 3 | 评测体系扩量 + CI 回归 | 🟡 重要 | 最小版已建（`eval/` 20 用例） |
| 4 | Token 级预算熔断 | 🟢 改进 | 当前只有调用次数上限 |
| 5 | 水平扩容 checkpoint（SQLite → Postgres） | 🟢 改进 | 多副本部署时换 `langgraph-checkpoint-postgres` |

---

*本报告基于 `production-hardening` 分支工作区状态维护；历史第三批明细见 git 历史（62f13ab）。*
