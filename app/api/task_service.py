"""
任务提交/取消/状态查询的模式切换层（P0-2 阶段 1）

把 server.py 中"任务全在 API 进程内"的调度逻辑整体收编到此模块，按
TASK_QUEUE_MODE 分流：

- inline（默认）：进程内 asyncio.create_task 直跑，行为与改造前完全一致；
- redis：任务写 Postgres tasks 表 + 入 ARQ 队列，由独立 worker 进程执行
  （app/queue/worker.py）。

HTTP 接口签名不变，server.py 只保留参数校验与复合键生成。错误处理风格
沿用现仓库：本层抛 TaskNotFoundError / 原生异常，server 层转 HTTPException。

与现有测试的兼容性约定：inline 分支调用的 run_deep_agent 以本模块的引用
为准（原 server.run_deep_agent 的 patch 点随逻辑一起迁移到
task_service.run_deep_agent）。
"""

import asyncio
import os
from typing import Any, Optional

from app.agent.main_agent import run_deep_agent
from app.queue.task_store import task_store
from app.utils.logging_setup import get_logger

logger = get_logger(__name__)

# 降级开关：未设置/inline = 进程内直跑（默认，行为零变化）；redis = 队列模式。
# 模块导入时读取（与 server.py 读取 CORS/限流环境变量的风格一致）
TASK_QUEUE_MODE = (os.getenv("TASK_QUEUE_MODE") or "inline").strip().lower() or "inline"
REDIS_URL = os.getenv("REDIS_URL") or "redis://localhost:6379/0"

# ARQ 任务函数名：与 worker.py 中 run_task 的函数名保持一致
QUEUE_JOB_NAME = "run_task"

# inline 模式的进程内任务登记（从 server.py 原样迁入）：
# 复合键 -> asyncio.Task，用于同一会话任务替换和主动取消
active_tasks: dict[str, asyncio.Task] = {}

# redis 模式的 ARQ 连接池（惰性初始化，进程内复用）
_arq_pool: Optional[Any] = None


class TaskNotFoundError(KeyError):
    """任务不存在或已结束：server 层捕获后统一转 404"""


def _forget_task(task_key: str, task: asyncio.Task) -> None:
    """
    清理已结束任务的登记关系（inline 模式，从 server.py 原样迁入）。

    done_callback 触发时，active_tasks 中可能已经被新任务替换；只有仍是同一个
    task 时才删除，避免误清理同复合键下刚启动的新任务。
    """
    if active_tasks.get(task_key) is task:
        active_tasks.pop(task_key, None)


def job_id_for(task_key: str, submit_count: int) -> str:
    """
    ARQ 入队去重键约定：f"task:{task_key}:{submit_count}"

    与 task_store.create_or_replace_task 的生成规则严格一致——固定 task_key
    会让 ARQ 按 _job_id 把新提交吞进旧 job（任务永远 pending），因此每次
    提交 submit_count+1 并重建 job_id。
    """
    return f"task:{task_key}:{submit_count}"


# ---------------------------------------------------------------------------
# 生命周期钩子（server.py lifespan 调用）
# ---------------------------------------------------------------------------

async def startup() -> None:
    """API 进程启动钩子：redis 模式下幂等建表，inline 模式为空操作"""
    if TASK_QUEUE_MODE == "redis":
        await task_store.init_schema()
        logger.info("[TaskService] 任务队列模式=redis，tasks 表已就绪")
    else:
        logger.info("[TaskService] 任务队列模式=inline（进程内直跑）")


async def shutdown() -> None:
    """API 进程退出钩子：释放 ARQ 连接池与 task 表连接池"""
    global _arq_pool
    if _arq_pool is not None:
        pool = _arq_pool
        _arq_pool = None
        # redis-py >=5 用 aclose()，旧版本是 close()：按可用性择一
        close = getattr(pool, "aclose", None) or pool.close
        try:
            await close()
        except Exception as e:  # pragma: no cover - 退出路径尽力清理
            logger.warning(f"[TaskService] 关闭 ARQ 连接池异常（忽略）: {e}")
    if TASK_QUEUE_MODE == "redis":
        await task_store.close()


async def _get_arq_pool() -> Any:
    """返回复用的 ARQ Redis 连接池，首次入队时创建"""
    global _arq_pool
    if _arq_pool is not None:
        return _arq_pool
    from arq import create_pool
    from arq.connections import RedisSettings

    _arq_pool = await create_pool(RedisSettings.from_dsn(REDIS_URL))
    logger.info(f"[TaskService] ARQ 连接池已创建: {REDIS_URL}")
    return _arq_pool


# ---------------------------------------------------------------------------
# inline 分支（从 server.py 原样迁入，行为零变化）
# ---------------------------------------------------------------------------

async def _submit_inline(query: str, task_key: str, user_id: str, thread_id: str) -> dict:
    # 同一用户同一 thread_id 只保留一个活跃任务；复合键保证跨租户互不影响
    old_task = active_tasks.get(task_key)
    if old_task and not old_task.done():
        old_task.cancel()

    # create_task 把长耗时 Agent 执行交给事件循环，接口本身不用等待最终结果
    task = asyncio.create_task(run_deep_agent(query, thread_id, user_id))
    active_tasks[task_key] = task
    task.add_done_callback(lambda finished_task: _forget_task(task_key, finished_task))
    return {"status": "started", "thread_id": thread_id}


async def _cancel_inline(task_key: str, thread_id: str) -> dict:
    task = active_tasks.get(task_key)
    if not task or task.done():
        active_tasks.pop(task_key, None)
        raise TaskNotFoundError("任务不存在或已结束")

    # 先发出取消信号，再短暂等待协程响应；若底层阻塞中，则返回 cancelling 给前端继续展示状态
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        _forget_task(task_key, task)
        return {"status": "cancelled", "thread_id": thread_id}
    except asyncio.TimeoutError:
        return {"status": "cancelling", "thread_id": thread_id}
    except Exception as e:
        _forget_task(task_key, task)
        return {"status": "cancelled", "thread_id": thread_id, "message": str(e)}

    _forget_task(task_key, task)
    return {"status": "cancelled", "thread_id": thread_id}


async def _status_inline(task_key: str, thread_id: str) -> dict:
    task = active_tasks.get(task_key)
    if not task or task.done():
        active_tasks.pop(task_key, None)
        raise TaskNotFoundError("任务不存在或已结束")
    # inline 模式没有 pending 阶段（create_task 即开始执行），活跃即视为 running
    return {"status": "running", "thread_id": thread_id}


# ---------------------------------------------------------------------------
# redis 分支（任务状态外置 + 执行出进程）
# ---------------------------------------------------------------------------

async def _submit_redis(query: str, task_key: str, user_id: str, thread_id: str) -> dict:
    # 写表拿 job_id（同 key 旧非终态行已被置 cancelled，对齐"同会话替换"语义）
    job_id = await task_store.create_or_replace_task(task_key, user_id, thread_id, query)
    pool = await _get_arq_pool()
    # _job_id 承担 ARQ 的 in-flight 去重：见 job_id_for 的约定说明
    await pool.enqueue_job(QUEUE_JOB_NAME, task_key, _job_id=job_id)
    logger.info(f"[TaskService] 任务已入队: {task_key} job_id={job_id}")
    return {"status": "started", "thread_id": thread_id}


async def _cancel_redis(task_key: str, thread_id: str) -> dict:
    prev_status = await task_store.cancel_task(task_key)
    if prev_status is None:
        # 与 inline 语义对齐：不存在或已终态 → 404
        raise TaskNotFoundError("任务不存在或已结束")
    if prev_status == "running":
        # worker 按取消轮询间隔（CANCEL_POLL_SECONDS）发现后中止，返回
        # cancelling 让前端继续展示取消中状态（与 inline 的阻塞取消语义一致）
        return {"status": "cancelling", "thread_id": thread_id}
    return {"status": "cancelled", "thread_id": thread_id}


async def _status_redis(task_key: str, thread_id: str) -> dict:
    row = await task_store.get_task(task_key)
    if row is None:
        raise TaskNotFoundError("任务不存在或已结束")
    return {
        "status": row["status"],
        "thread_id": thread_id,
        "submit_count": row["submit_count"],
        "worker_id": row["worker_id"],
        "error": row["error"],
    }


# ---------------------------------------------------------------------------
# 对外统一入口（server.py 调用）
# ---------------------------------------------------------------------------

async def submit_task(query: str, task_key: str, user_id: str, thread_id: str) -> dict:
    """启动一次 Agent 任务：inline 直跑 / redis 入队，HTTP 响应结构一致"""
    if TASK_QUEUE_MODE == "redis":
        return await _submit_redis(query, task_key, user_id, thread_id)
    return await _submit_inline(query, task_key, user_id, thread_id)


async def cancel_task(task_key: str, thread_id: str) -> dict:
    """取消指定复合键的任务；不存在或已终态抛 TaskNotFoundError（server 层转 404）"""
    if TASK_QUEUE_MODE == "redis":
        return await _cancel_redis(task_key, thread_id)
    return await _cancel_inline(task_key, thread_id)


async def get_task_status(task_key: str, thread_id: str) -> dict:
    """查询任务状态：inline 从 active_tasks 推断，redis 查任务表"""
    if TASK_QUEUE_MODE == "redis":
        return await _status_redis(task_key, thread_id)
    return await _status_inline(task_key, thread_id)
