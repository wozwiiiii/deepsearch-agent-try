# deepsearch-agents 工作状态报告

> **更新时间**：2026-09-05
> **分支**：`main`（仓库唯一分支，**无 `production-hardening` 分支**）
> **HEAD**：`4aebb83`
> **远端**：`origin` = `wozwiiiii/deepsearch-agent-try`；本地 `main` 领先 `origin/main`（`f4f1995`）**7 个提交，已提交未推送**
> **工作区**：干净（`git status` 无输出）

---

## 一、仓库来源与增量口径（2026-09-05 实测更正）

**此前文档称"增量在 `production-hardening` 分支、`git diff main` 可看全部增量"——该说法已失效，实测更正如下：**

| 项 | 实测 | 说明 |
|----|------|------|
| 分支 | 仅 `main` | `git branch -a` 只有 `main` 与 `origin/main`；`production-hardening` **不存在** |
| 上游基线 | `df6d52e`（didilili，2026-05-18） | 上游最后一笔；此前 20 笔作者均为 didilili |
| 本人增量 | 18 笔（`d0f6eed` → `4aebb83`） | 全量 38 笔 = didilili 20 + wozwiiiii 18 |
| 查看增量 | `git diff df6d52e..HEAD` | **`git diff main` 恒为空**（增量已在 main 上，不可再用） |

**增量规模（多口径，引用时须指明口径）**：

| 口径 | 命令 | 结果 |
|------|------|------|
| 全量增量 | `git diff --shortstat df6d52e..HEAD` | 53 文件，**+6193 / −546** |
| 生产化改造（自 `70f3162` 起） | `git diff --shortstat 70f3162^..HEAD` | 51 文件，+6191 / −544 |
| 排除 lock 文件 | 同上 + `':(exclude)uv.lock'` | 52 文件，+6025 / −543 |
| 本轮 7 笔 | `git diff --shortstat c11fd2b^..HEAD` | 17 文件，**+531 / −116** |

> 此前文档写的"40 文件 / +3649 / −209"是中间时点的旧统计，已由上表取代。

---

## 二、提交脉络（38 笔，时间正序）

### 2.1 上游底座（didilili，20 笔）

`9d92662`（2026-05-08 首次提交）→ … → `df6d52e`（2026-05-18"正式上线"）。对应教程"深度研搜"实战。

### 2.2 本人增量（18 笔）

| 提交 | 内容 | 批次 |
|------|------|------|
| `d0f6eed` | 修正错误的文件夹命名 | 前置 |
| `70f3162` | **第一批** 安全加固：路径穿越 / SQL 只读 / 接口会话隔离，76 项安全测试 | 第一批 |
| `d2bc74e` | **第二批** API Key 认证 + 多租户复合键隔离 + SQLite 持久化 checkpointer | 第二批 |
| `62f13ab` | **第三批** 接口限流 + fail-closed + 模型调用上限 + 审查修复 | 第三批 |
| `3ccd40b` | UNION 等集合操作补 LIMIT + 测试数同步 125→129 | 修复 |
| `9f04b45` | 评测集最小版 + P0-2/P0-3 设计方案 + 使用纠错指南 | 第四批前 |
| `6b4a5dc` | **第四批** P0-3 事件回放 + 任务硬超时 + 审查修复 | 第四批 |
| `f741fb6` | P1-4a 单任务 token 预算熔断 | 增量 |
| `2a0db27` | P1-2 短时链接令牌（密钥不进 URL） | 增量 |
| `64c8aa9` / `4d41d02` / `f4f1995` | CODE_WIKI 文档、README 更新、CI 处理 | 文档 |
| `c11fd2b` | **P0-3** 修正主智能体提示词与真实工具能力的矛盾 | 本轮 |
| `c0b5b45` | **P0-4** 同步 I/O 与 LLM 调用加超时/重试，防线程池耗尽 | 本轮 |
| `9506b8a` | **P1** 统一结构化日志，trace_id 贯穿 | 本轮 |
| `66f85e9` | 评测集 20 → 45 条 | 本轮 |
| `b1d7774` | 修正提示词"你你"笔误 | 本轮 |
| `3e91737` | CI 接入评测集结构自检 + 重复 id 断言 | 本轮 |
| `4aebb83` | 设计文档 Markdown 格式化（无内容变化） | 本轮 |

### 2.3 本轮 7 笔细节（领先远端，未推送）

| 提交 | 文件 | 关键改动 |
|------|------|----------|
| `c11fd2b` | `app/prompt/prompts.yml` | 主智能体 system_prompt 三处矛盾：删除指向不存在的"文件生成助手"的路由、移除无对应工具的 Word 承诺、消除"不能自己生成"与"工具在你手上"的自我否定。与 `main_agent.py` 真实 `tools=[generate_markdown, convert_md_to_pdf, read_file_content]` 对齐 |
| `c0b5b45` | `tavily_tool.py` / `llm.py` / `db_tools.py` | Tavily `timeout=20`；`init_chat_model(max_retries=2)`；MySQL `connection_timeout=10`。零新依赖 |
| `9506b8a` | `logging_setup.py`(新) / `context.py` / 6 个调用方 | `JsonFormatter` + `TraceContextFilter`（contextvars 注入 trace_id/user_id/thread_id，缺失填 `-`）；`reset_session_context` 加可选 token 参数保持向后兼容 |
| `66f85e9` | `eval/cases.py` | 20 → 45 条：sql 22 / routing 6 / web 10 / multi 7。既有 20 条零改动 |
| `b1d7774` | `app/prompt/prompts.yml` | 第 33 行"你你掌握的工具"→"你掌握的工具" |
| `3e91737` | `.github/workflows/ci.yml` / `eval/cases.py` | CI 加 `python eval/cases.py` 步骤；自检补重复 id 断言 |
| `4aebb83` | `EVENT_REPLAY_DESIGN.md` / `TASK_QUEUE_DESIGN.md` | 纯格式化 |

---

## 三、代码基线与验证结果（2026-09-05 实测）

| 层次 | 规模 | 验证 |
|------|------|------|
| `app/` 后端 | 28 个 `.py` / **3,351 行** | — |
| `tests/` | 9 个 `.py` / 1,536 行 | **154 用例全部通过**，实测 **28.80s** |
| `eval/` | 4 个 `.py` / 767 行 | **45 条结构自检通过** |
| `frontend/src/` | 19 个 `.tsx/.ts` / 2,140 行 | — |

> 此前文档写"154 个测试约 10 秒"，实测 **28.80 秒**（`.venv/Scripts/python.exe -m pytest tests/ -q`）。面试表述建议改为"半分钟内"。

**测试分布**：路径 14 / SQL 45 / 接口 31 / 认证与租户·令牌 35 / 限流 7 / 持久化·超时·预算 8 / 事件回放 14 = 154。

**服务端点（8 个）**：`GET /health`、`POST /api/token`、`POST /api/task`、`POST /api/task/{thread_id}/cancel`、`POST /api/upload`、`GET /api/files`、`GET /api/download`、`WS /ws/{thread_id}`。

---

## 四、能力完成度（15 项，已完成 12）

| 域 | 完成 | 明细 |
|----|------|------|
| 安全加固 | **6/6** | 路径收容、SQL 三层防护、认证 fail-closed、会话隔离、限流、短时令牌 |
| 状态与回放 | **2/2** | Checkpoint 持久化、事件回放（seq + last_seq 补发） |
| 成本与熔断 | **1/1** | 600s 硬超时 + 80 次调用上限 + 150 万 token 预算 |
| 可观测性 | **1/2** | 结构化日志已落地；**OTel / 指标采集未做** |
| 质量体系 | **3/3** | 评测集 45 条、CI 结构自检已接；**四轮曲线：基线 30/45（66.7%）→ v2 36/45（80.0%）→ v3 40/45（88.9%）→ v4 41/45（91.1%，routing/web/multi 三类满分）**，见 `EVAL_COMPARISON*.md` 系列 |
| 部署架构 | **0/1** | **P0-2 任务出进程未实现** |

---

## 五、两项易被误判的事实（2026-09-05 澄清）

### 5.1 生产路径 `print` 已清零

`grep -rn 'print(' app/` 命中 12 处，但**逐行核对后全部位于 `if __name__ == "__main__":` 演示块内**，不在生产路径：

| 文件 | print 行号 | `__main__` 行号 |
|------|-----------|----------------|
| `db_tools.py` | 364 | 362 |
| `markdown_tools.py` | 89 / 94 / 97 | 80 |
| `pdf_tools.py` | 108 | 72 |
| `tavily_tool.py` | 70 | 66 |
| `upload_file_read_tool.py` | 130 / 131 / 136 / 137 | 121 |
| `ragflow_tools.py` | 120 / 121 | 已注释 |

**结论：结构化日志改造完整，生产路径 `print` 零残留。** 此前"print 未覆盖"的判断不成立。

### 5.2 CI 接入的是"结构自检"，不是全量评测

`.github/workflows/ci.yml` 执行 `python eval/cases.py`，校验用例 id 唯一性与字段完整性，**不调用真实 Agent**。全量评测需 `OPENAI_API_KEY` + `TAVILY_API_KEY` + MySQL，不稳定且产生费用，刻意不进 CI。

---

## 六、未完成项与阻塞条件

| 优先级 | 任务 | 状态 | 阻塞条件 |
|--------|------|------|----------|
| **P0** | P0-2 任务出进程 | **一行未动** | 设计稿 `TASK_QUEUE_DESIGN.md` 完备；需 Redis，估 4–6 天 |
| ~~P0~~ | ~~评测基线跑分~~ | **已完成（2026-09-05）** | 首轮基线 30/45（66.7%，剔除 4 条污染用例 29/41），成本 ¥0.98。报告：`EVAL_BASELINE_2026-09-05.md` |
| ~~P1~~ | ~~主智能体早停防护~~ | **已完成+实战验证（2026-09-06）** | 守卫实战：过渡语早停 2 次触发 2 次救回（sql-05/route-01）；剔污染通过率 40/41（97.6%）。**新暴露空输出早停盲区（route-02/multi-07，n=2）→ 列入 P1 待办** |
| ~~P2~~ | ~~文件生成工具层硬约束~~ | **已完成（2026-09-06）** | `file_intent_guard.py`：generate_markdown/convert_md_to_pdf 入口校验用户消息文件意图（双条件 AND，fail-open）；QA 独立验证 45 条评测 query 零误拒；201 单测全绿。真实评测效果待 v4 验证 |
| ~~P1~~ | ~~空输出早停守卫扩展~~ | **已完成（2026-09-06）** | `guard_trigger_reason` 统一判定：空输出（content 空/None 且非工具片段截断）触发守卫续跑；工具片段截断仍不介入（防重复执行工具）；206 单测全绿。真实评测效果待 v4 验证 |
| P2 | QA 遗留清理 | **部分完成（2026-09-06）** | ✅ 429 检测收窄为数字边界正则；✅ web-07 测试句与评测原句对齐；⬜ 守卫短语表"清单/汇总"边界（评估：当前 45 条零误拒且方向为放行，无真实误伤样本驱动，暂缓） |
| ~~P1~~ | ~~移除 `api_key` 查询参数兼容入口~~ | **已完成（2026-09-06）** | WS/直链/限流键三处查询参数回退删除，前端 catch 保底发送移除；兼容期测试语义反转为"已拒绝"锁定；206 单测全绿 |
| ~~P1~~ | ~~会话级 token 预算~~ | **已完成（2026-09-06）** | `token_budget.py`：按 thread_id 跨任务累计，超 450 万（默认 3×run 级）熔断；单进程边界如实注明（P0-2 后需迁移共享存储）；212 单测全绿 |
| P1 | 移除 `api_key` 查询参数兼容入口 | 未做 | 前端已不发送 |
| P2 | OTel / Prometheus | 未做 | 结构化日志是其前置，已就位 |
| P2 | 事件库 TTL 清理 | 未做 | 当前只有条数裁剪（每 task_key 保留最近 1000 条） |

**当前最大结构性缺口**：P0-2。任务执行仍为 `asyncio.create_task`（`server.py:348`）跑在 Web 进程内——进程重启即丢失进行中任务，且无法水平扩容。

---

## 七、企业级差距路线图（长期）

| # | 差距 | 阻塞性 | 现状 |
|---|------|--------|------|
| 1 | 任务队列与并发治理（P0-2） | 🔴 上线必须 | `create_task` → 需外置队列；事件存储随本批换 Redis Stream |
| 2 | 可观测性（OTel + Prometheus） | 🟡 重要 | 结构化日志已就位，OTel 未接 |
| 3 | 评测体系跑分 + 回归 | 🟢 首轮已完成 | 2026-09-05 基线 30/45（66.7%）；后续为按修复迭代重跑对比 |
| 4 | 水平扩容 checkpoint | 🟢 改进 | SQLite → Postgres（多副本部署时换 `langgraph-checkpoint-postgres`） |

---

*本报告数据来自 2026-09-05 实测命令输出（git / grep / pytest / eval 自检），未采信任何文档的自我陈述。引用前请以 `git log` 与命令实测为准。*
