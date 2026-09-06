# P0-2 任务队列与并发治理设计方案

> 状态：**阶段 1 已实现**（2026-09-06，提交 `33761bc`）。阶段 2（事件广播）待做。
>
> **2026-09-06 实现落地**：阶段 1（任务状态出进程）已交付并经 QA 两轮回归（Round 1 判返工两项缺陷，Round 2 真库探针重放验证闭环）。实际落地与本稿的差异（以代码为准）：
> - **`TASK_QUEUE_MODE=inline|redis` 双模式降级开关**（本稿未写）：默认 inline = 原进程内直跑（存量测试/评测/开发零依赖零变化）；redis = 本稿阶段 1 全链路。实现于 `app/api/task_service.py`。
> - **task 表增加 `submit_count`/`job_id` 列**（本稿未覆盖的坑）：ARQ 用 `_job_id` 做 in-flight 去重，固定 task_key 会导致"旧 job 吞掉新提交、任务永远 pending"；每次提交 `submit_count+1` 并以 `f"task:{task_key}:{submit_count}"` 重建 job_id，重拾逻辑确定性重建同 id。
> - **worker 代际守卫**（QA Round 1 发现 P0-1 后补强）：mark_finished/cancel 收尾 WHERE 携带 worker_id——迟到的旧代际 worker 无法污染新提交的行；`_watch_cancellation` 升级为取消/替换观察协程（行回 pending / worker_id 换人 / 终态被他方写入 → 中止旧 run）。
> - **崩溃恢复**：recover_unfinished 对 running 行先 `reset_running`（清 worker_id/started_at）再入队（QA Round 1 发现 P1-1：重入队后被 run_task 的 pending 检查 skip，已修）。
> - **WS 事件轮询桥**：worker 事件经共享 event_store 落库，API 进程 WS 连接每 1s `read_after` 推差量（monitor.py 零改动）——本稿阶段 2 换 Redis pub/sub 时，此轮询任务是唯一替换点。
> - 启动方式见 `PRODUCTION_NOTES.md`（`arq app.queue.worker.WorkerSettings`）；测试 262 个（真容器全量）。
>
> 阶段 0（会话亲和）：跳过（纯运维配置，无代码价值）。

## 一、当前架构为什么不能多副本

三个组件都绑定"单进程"假设：

```
当前（单进程）
┌─────────────────────────────────────────────────────┐
│  API 进程（uvicorn）                                  │
│  ┌──────────┐   active_tasks: dict (进程内)          │
│  │ /api/task│ → asyncio.create_task(run_deep_agent)  │
│  └──────────┘   ┌──────────────────────────┐         │
│                 │ monitor → WebSocket(本进程)│        │
│                 └──────────────────────────┘         │
│   AsyncSqliteSaver → app/data/checkpoints.sqlite3   │
└─────────────────────────────────────────────────────┘
```

| 组件                       | 多副本下的问题                                          |
| ------------------------ | ------------------------------------------------ |
| `active_tasks`（进程内 dict） | 取消请求打到没该任务的 worker → 404                         |
| WS 连接粘单进程                | 任务在 worker A 执行，事件只能推到 A 的 WS；连到 worker B 的前端收不到 |
| `AsyncSqliteSaver` 单文件   | 多进程并发写同一 SQLite → 库级写锁冲突                         |

## 二、目标架构

```
                ┌─────────────┐
   前端 ──HTTP──▶│  API 进程 ×N │  无状态：只收请求、写任务表、订阅事件
                └──────┬──────┘
                       │ 1. 写 task 表（status=pending）
                       │ 2. 入队（Redis Stream / ARQ）
                       ▼
                ┌─────────────┐    ┌───────────────┐
                │  Redis       │◀──│  Worker 进程   │  跑 run_deep_agent
                │  (队列+pubsub)│   │  (ARQ worker)  │  事件发到 Redis pub/sub
                └──────┬──────┘    └───────┬───────┘
                       │                   │ 3. 执行中写 task 表状态
                       │ pub/sub 事件       │ 4. 事件 publish 到 channel
                       ▼                   │
                ┌─────────────┐            │
                │  API 进程    │◀───────────┘  订阅 channel → 推 WS
                │ (WS 持有者)  │
                └─────────────┘
                       │
                       ▼
                ┌─────────────┐
                │  Postgres    │  task 表 + checkpointer（替换 SQLite）
                └─────────────┘
```

**关键不变量**：HTTP 接口签名不变，只换实现；复合键 `{user_id}-{thread_id}` 仍是 task 表主键、WS 路由键、checkpointer thread\_id。

## 三、任务表 schema（Postgres）

```sql
CREATE TABLE tasks (
    task_key      VARCHAR(64) PRIMARY KEY,          -- {user_id}-{thread_id}，与现有一致
    user_id       VARCHAR(15) NOT NULL,
    thread_id     VARCHAR(48) NOT NULL,
    query         TEXT NOT NULL,
    status        VARCHAR(16) NOT NULL DEFAULT 'pending',
                   -- pending | running | done | failed | cancelled
    worker_id     VARCHAR(64),                       -- 哪个 worker 在跑（取消时定位用）
    result        TEXT,                              -- 最终答案（done 时写）
    error         TEXT,
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_user ON tasks(user_id);
```

* 取消接口不再查 `active_tasks` dict，而是 `UPDATE tasks SET status='cancelled' WHERE task_key=...`；worker 轮询状态发现 cancelled 后中止。

* 重启不丢任务：`pending`/`running` 的任务由 worker 启动时重拾（`running` 重新入队，幂等由 task\_key 保证）。

## 四、队列选型：ARQ（推荐）

| 方案       | 优点                                    | 缺点                                 | 适配度   |
| -------- | ------------------------------------- | ---------------------------------- | ----- |
| **ARQ**  | 纯 async、基于 Redis、轻量、与 FastAPI 同事件循环模型 | 功能比 Celery 少                       | ★★★★★ |
| Celery   | 成熟、生态大                                | 同步模型，与 async Agent 混用要起独立进程+线程池，复杂 | ★★★   |
| Temporal | 工作流引擎、可观测强                            | 引入整套 Temporal server，过重            | ★★    |

**推荐 ARQ**：本项目 Agent 是 async（`run_deep_agent` 是协程），ARQ 原生 async、只需 Redis、worker 与 API 同样是 asyncio。Celery 的同步模型会让 async Agent 退化成线程池调用，丢掉并发优势。

## 五、与现有代码的接口对齐（最小改动点）

| 现有                                     | 改造后                                                      | 改动位置                                         |
| -------------------------------------- | -------------------------------------------------------- | -------------------------------------------- |
| `active_tasks: dict`                   | 删掉，改查 `tasks` 表                                          | `server.py`                                  |
| `asyncio.create_task(run_deep_agent)`  | `await pool.enqueue_job('run_task', query, task_key)`    | `server.py::run_task`                        |
| `cancel_task` 查 dict + `task.cancel()` | `UPDATE tasks SET status='cancelled'`，worker 轮询中止        | `server.py` + worker                         |
| `AsyncSqliteSaver`                     | `langgraph-checkpoint-postgres` 的 `AsyncPostgresSaver`   | `main_agent.py::_get_agent`（接口不变，换 saver 实现） |
| `monitor._send_to_websocket` 直推本进程 WS  | `redis.publish(channel, payload)`；API 进程订阅 channel 再推 WS | `monitor.py`                                 |

**复合键不动**：`composite_task_key`、`validate_thread_id`、user\_id 字符集约束全部保留——这是多租户隔离的根，改它等于推倒重来。

## 六、迁移步骤（阶梯式，可灰度）

### 阶段 0：会话亲和过渡（1 天，上线最小可用）

* 负载均衡做 sticky session（按 `X-API-Key` 或 `thread_id` 哈希固定到某 worker）；

* 保持单进程语义，`active_tasks`/SQLite 仍可用；

* **价值**：先能多副本跑起来抗故障，不解决并发写问题。

* **风险**：单 worker 挂了它上面的任务全丢（可接受过渡期风险）。

### 阶段 1：任务状态出进程（2-3 天）

* 建 Postgres + task 表；

* `run_task` 改为写 task 表 + 入 ARQ 队列；新增 `worker.py` 跑 ARQ worker 调 `run_deep_agent`；

* `cancel_task` 改为更新 task 表状态；

* checkpointer 换 `AsyncPostgresSaver`。

* **价值**：任务持久、取消跨副本生效、checkpointer 多副本安全。

### 阶段 2：事件广播（1-2 天）

* `monitor` 的事件改 publish 到 Redis `channel:{task_key}`；

* API 进程启动时订阅用户相关 channel，收到事件推 WS；

* 与 P0-3 事件回放天然合并（事件落 Redis Stream 既能广播又能回放，见 EVENT\_REPLAY\_DESIGN.md）。

## 七、风险与取舍

| 风险              | 说明                                 | 缓解                                                                               |
| --------------- | ---------------------------------- | -------------------------------------------------------------------------------- |
| Postgres 引入运维成本 | 要起 DB、备份                           | 单机可先用 Docker；上线用云托管                                                              |
| ARQ worker 单点   | worker 挂任务卡 running                | 启动时扫 running 任务重入队 + 心跳超时回收                                                      |
| 取消延迟            | 不再是即时 `task.cancel()`，要等 worker 轮询 | worker 每 1-2s 检查 task 状态；取消返回 `cancelling` 而非 `cancelled`（与现有 `cancelling` 状态一致） |
| 复合键碰撞           | 极低概率                               | user\_id 字符集约束已保证无歧义                                                             |

## 八、面试讲法

> "我把任务从进程内 asyncio.create\_task 改成 ARQ 队列 + Postgres task 表 + Redis pub/sub：HTTP 进程无状态可水平扩容，worker 独立进程跑 Agent，事件经 Redis 广播给持有 WS 的 API 进程。checkpointer 从 SQLite 换 Postgres（接口不变，当初选可替换 saver 的回报）。复合键 `{user}-{thread}` 全程不动——它是多租户隔离的根。迁移分三阶段：会话亲和过渡 → 状态出进程 → 事件广播，每阶段都能独立上线。"

## 九、工作量估计

* 阶段 0：1 天

* 阶段 1：2-3 天

* 阶段 2：1-2 天（与 P0-3 合并做更划算）

* **合计 4-6 天**（含测试）

