"""
ARQ worker：把 run_deep_agent 的执行搬出 API 进程（P0-2 阶段 1）

启动命令（详见 docs/status/PRODUCTION_NOTES.md）：
    arq app.queue.worker.WorkerSettings

设计要点：
1. 任务函数只传 task_key——query/user_id/thread_id 执行时从任务表读取
   （单一事实源），Redis 队列里不冗余任务参数；
2. 状态 != pending 则跳过：覆盖"入队后被取消/替换"的竞态窗口；
3. 每 CANCEL_POLL_SECONDS（默认 2s）轮询任务表，发现 cancelled（用户取消）
   或代际变更（任务被新一轮提交替换，QA 回归 P0-1）后取消内层 asyncio task，
   并等待 run_deep_agent 的 CancelledError 清理分支（monitor 上报
   task_cancelled、恢复 ContextVar）跑完再尝试落终态——落终态携带
   worker_id 代际身份，被替换时由 task_store 守卫拒绝，不污染新行；
4. 不自动重试（max_tries=1）——Agent 任务重试是真实花费且副作用非幂等，
   失败已落任务表（error 字段），可人工重提；
5. 启动时重拾 pending/running 任务：running 先重置为 pending（QA 回归
   P1-1），再按 (task_key, submit_count) 确定性重建 job_id 重入队，
   worker 重启不丢任务。
"""

import asyncio
import os
import socket
import sys
import uuid

from arq.connections import RedisSettings

from app.agent.main_agent import (
    TASK_TIMEOUT_SECONDS,
    close_main_agent,
    run_deep_agent,
)
from app.queue.task_store import task_store
from app.utils.logging_setup import get_logger

logger = get_logger(__name__)

# Windows 下 psycopg 异步模式要求 SelectorEventLoop：默认 ProactorEventLoop
# 不支持 add_reader，连接池建连会失败（实测踩中）。必须在事件循环创建前设置，
# 放在模块导入时——arq 导入本模块拿到 WorkerSettings 后才 asyncio.run
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 与 task_service.QUEUE_JOB_NAME 保持一致（ARQ 函数名默认取函数 __name__）
QUEUE_JOB_NAME = "run_task"
QUEUE_NAME = "deepsearch:tasks"
REDIS_URL = os.getenv("REDIS_URL") or "redis://localhost:6379/0"

# 取消轮询间隔：取消请求经任务表生效，worker 按此间隔检查后中止执行
CANCEL_POLL_SECONDS = float(os.getenv("CANCEL_POLL_SECONDS") or "2")
# 单 worker 并发任务数上限
WORKER_MAX_JOBS = int(os.getenv("WORKER_MAX_JOBS") or "2")
# 收到取消信号后，等 run_deep_agent 清理分支（monitor 上报等）跑完的宽限时间
CANCEL_GRACE_SECONDS = 10.0
# ARQ job 超时：略大于任务硬超时（TASK_TIMEOUT_SECONDS 默认 600s），
# 留出状态落表的余量；超时由 job_timeout 兜底防止任务卡死队列


def _worker_id() -> str:
    """当前 worker 实例标识：host:pid:随机后缀，落任务表用于取消定位与排查"""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def job_id_for(task_key: str, submit_count: int) -> str:
    """
    ARQ 入队去重键约定：f"task:{task_key}:{submit_count}"

    与 task_store.create_or_replace_task / task_service.job_id_for 严格一致：
    重拾（recover_unfinished）按同一规则确定性重建 job_id。
    """
    return f"task:{task_key}:{submit_count}"


async def _watch_cancellation(task_key: str, worker_id: str) -> str:
    """
    取消/替换观察协程：按 CANCEL_POLL_SECONDS 轮询任务表，发现本 worker
    不再持有该任务即返回，与执行协程喂给 asyncio.wait(FIRST_COMPLETED)。

    :returns: 'cancelled'（用户取消）或 'replaced'（任务被新一轮提交替换 /
        终态已由他方写入 / 被崩溃恢复重置）——代际判定（QA 回归 P0-1）：
        - status == 'cancelled'：用户取消；
        - status == 'pending'：本 worker 已领取（mark_running 成功）后行又
          回到 pending，只可能是 create_or_replace 替换或 recover 重置；
        - status == 'running' 且 worker_id != 本 worker：替换后已被新
          worker 领取；
        - status 为终态：终态已被他方写入（本轮执行结果不再有意义）。
    """
    while True:
        await asyncio.sleep(CANCEL_POLL_SECONDS)
        try:
            row = await task_store.get_task(task_key)
        except Exception as e:
            # 单次轮询失败（网络抖动等）不中断观察，下一轮重试
            logger.warning(f"[Worker] 取消轮询读取失败（下轮重试）: {task_key}: {e}")
            continue
        if row is None:
            continue
        status = row["status"]
        if status == "cancelled":
            return "cancelled"
        if status == "pending":
            return "replaced"
        if status == "running" and row["worker_id"] != worker_id:
            return "replaced"
        if status in ("done", "failed"):
            return "replaced"


async def run_task(ctx: dict, task_key: str) -> str:
    """
    ARQ 任务函数：执行一次 DeepAgents 任务

    :param ctx: ARQ 提供的运行上下文（含 redis 连接）
    :param task_key: 复合键 {user_id}-{thread_id}，任务参数从任务表读取
    :returns: 执行结果摘要（"done|failed|cancelled|replaced|stale|skipped:"
        "{task_key}"；replaced/stale = 执行期间任务被替换，代际守卫拒绝落
        终态），主要供 arq results 与日志排查用，前端状态以任务表为准
    """
    row = await task_store.get_task(task_key)
    if row is None or row["status"] != "pending":
        # 入队后被取消/替换（状态已非 pending）：跳过执行，不落任何终态
        # （终态已由取消方/替换方写入）
        logger.info(f"[Worker] 跳过非 pending 任务: {task_key}")
        return f"skipped:{task_key}"

    worker_id = _worker_id()
    if not await task_store.mark_running(task_key, worker_id):
        # get_task 与 mark_running 之间的窄竞态：恰在此期间被取消/替换
        logger.info(f"[Worker] 领取任务失败（已被取消/替换）: {task_key}")
        return f"skipped:{task_key}"

    logger.info(
        f"[Worker] 开始执行任务: {task_key} worker_id={worker_id} "
        f"thread_id={row['thread_id']} user_id={row['user_id']}"
    )

    # 执行协程：wait_for 对整个执行（含 Agent 惰性初始化）施加硬超时，
    # 与 API 进程内直跑的现有语义对齐（对齐 main_agent.TASK_TIMEOUT_SECONDS）
    exec_task = asyncio.ensure_future(
        asyncio.wait_for(
            run_deep_agent(row["query"], row["thread_id"], row["user_id"]),
            timeout=TASK_TIMEOUT_SECONDS,
        )
    )
    watch_task = asyncio.create_task(_watch_cancellation(task_key, worker_id))

    done, _pending = await asyncio.wait(
        {exec_task, watch_task}, return_when=asyncio.FIRST_COMPLETED
    )

    if watch_task in done and not exec_task.done():
        # 用户取消（cancelled）或任务已被新一轮提交替换/终态被他人写入
        # （replaced，QA 回归 P0-1）：中止执行并等待 run_deep_agent 的
        # CancelledError 清理分支跑完（monitor 上报、恢复 ContextVar），
        # 再尝试落终态——mark_finished 的 worker_id 代际守卫保证只有仍
        # 持有本轮任务的 worker 才能写入，替换场景下自然被拒绝
        reason = watch_task.result()
        exec_task.cancel()
        try:
            await asyncio.wait_for(exec_task, timeout=CANCEL_GRACE_SECONDS)
        except asyncio.CancelledError:
            pass  # 预期路径：run_deep_agent 清理分支完成后重新抛出
        except (asyncio.TimeoutError, Exception) as e:
            # 清理分支异常不吞掉取消语义，仅留痕
            logger.warning(f"[Worker] 取消清理阶段异常（忽略）: {task_key}: {e}")
        finished = await task_store.mark_finished(
            task_key, "cancelled", worker_id=worker_id
        )
        if finished:
            logger.info(f"[Worker] 任务已取消: {task_key}")
        else:
            logger.info(
                f"[Worker] 放弃落终态（任务已被新一轮提交替换，"
                f"代际守卫拒绝）: {task_key}"
            )
        return f"{reason}:{task_key}"

    # 正常收尾：停掉取消观察协程
    watch_task.cancel()
    try:
        await watch_task
    except asyncio.CancelledError:
        pass

    try:
        # run_deep_agent 的返回值是本次任务的 token 消耗；最终答案经
        # monitor → 共享 event_store → WS 到达前端，不在任务表重复存储
        tokens_used = await exec_task
        finished = await task_store.mark_finished(
            task_key, "done", result=f"tokens_used={tokens_used}", worker_id=worker_id
        )
        if finished:
            logger.info(f"[Worker] 任务完成: {task_key} tokens_used={tokens_used}")
            return f"done:{task_key}"
        # 代际守卫拒绝：执行期间任务被替换（新提交/恢复重置），本轮结果作废
        logger.warning(
            f"[Worker] 落终态被代际守卫拒绝（执行期间任务已被替换）: {task_key}"
        )
        return f"stale:{task_key}"
    except Exception as e:
        # 失败不重试（max_tries=1）：错误落表，人工确认后重提
        finished = await task_store.mark_finished(
            task_key, "failed", error=str(e), worker_id=worker_id
        )
        if finished:
            logger.error(f"[Worker] 任务失败: {task_key}: {e}")
            return f"failed:{task_key}"
        logger.warning(
            f"[Worker] 落终态被代际守卫拒绝（执行期间任务已被替换）: {task_key}: {e}"
        )
        return f"stale:{task_key}"


async def recover_unfinished(ctx: dict) -> None:
    """
    worker 启动钩子（on_startup）：重拾任务表中的 pending/running 任务

    场景：worker 重启/崩溃时，已入队未执行（pending）与执行中断（running）
    的任务全部重新入队。job_id 按 (task_key, submit_count) 确定性重建——
    与提交时的 job_id 相同，ARQ 的 _job_id 去重保证同一轮任务不会重复执行。

    running 行先重置为 pending（清 worker_id/started_at，QA 回归 P1-1）：
    否则 run_task 的"非 pending 即跳过"会让重入队永远空转、任务卡死
    running。重置后原 worker 若仍存活（僵尸执行），其收尾会被 mark_finished
    的 worker_id 代际守卫拒绝，不会污染重置后的新一轮执行。
    """
    await task_store.init_schema()
    rows = await task_store.list_unfinished()
    if not rows:
        logger.info("[Worker] 启动重拾：无未完成任务")
        return
    redis = ctx["redis"]
    for row in rows:
        task_key = row["task_key"]
        if row["status"] == "running":
            await task_store.reset_running(task_key)
        job_id = job_id_for(task_key, row["submit_count"])
        await redis.enqueue_job(QUEUE_JOB_NAME, task_key, _job_id=job_id)
        logger.info(
            f"[Worker] 启动重拾: {task_key} "
            f"status={row['status']} job_id={job_id}"
        )


async def shutdown_worker(ctx: dict) -> None:
    """worker 退出钩子（on_shutdown）：释放 Agent/checkpointer 连接与任务表连接池"""
    await close_main_agent()
    await task_store.close()
    logger.info("[Worker] 已退出（连接已释放）")


class WorkerSettings:
    """
    ARQ worker 配置（启动命令：arq app.queue.worker.WorkerSettings）
    """

    functions = [run_task]
    queue_name = QUEUE_NAME
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
    on_startup = recover_unfinished
    on_shutdown = shutdown_worker
    # 略大于任务硬超时（TASK_TIMEOUT_SECONDS 默认 600s），留出落表余量
    job_timeout = 660
    # Agent 任务重试是真实花费且副作用非幂等：失败落表，人工重提
    max_tries = 1
    max_jobs = WORKER_MAX_JOBS
