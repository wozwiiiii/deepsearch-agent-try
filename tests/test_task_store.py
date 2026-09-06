"""
task_store 集成测试（P0-2 阶段 1）：真实 Postgres 验证

验证内容：幂等 DDL、创建/查询往返、submit_count/job_id 约定、
同 key 替换语义、状态机守卫（mark_running 仅 pending、mark_finished
不可覆盖终态）、取消返回原状态、list_unfinished 过滤。

运行条件：本机起 PG 容器（docker compose up -d postgres）并设置
POSTGRES_DSN。未设置或连不上时整文件跳过——不阻塞 inline 默认路径的
回归测试，也不发任何 LLM 调用。
"""

import asyncio
import os
import threading
import uuid

import pytest

from app.queue.task_store import PostgresTaskStore

_DEFAULT_DSN = "postgresql://deepsearch:deepsearch@localhost:5433/deepsearch_tasks"
_DSN = os.getenv("POSTGRES_DSN") or ""

# 整个测试文件共用一个常驻事件循环（后台线程驱动）：
# 1. Windows 下 psycopg 异步模式要求 SelectorEventLoop（默认 Proactor 不支持）；
# 2. AsyncConnectionPool 的后台连接任务绑定创建它的事件循环，若每个协程各用
#    一个 asyncio.run 临时循环，teardown 的 pool.close() 会在异循环上挂死
_loop = asyncio.SelectorEventLoop()
_loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
_loop_thread.start()


def _run_async(coro):
    """在常驻 selector 循环上执行协程并同步等待结果"""
    return asyncio.run_coroutine_threadsafe(coro, _loop).result()


def _unique_key(prefix: str) -> str:
    """复合键必须 ≤64 位且字符集与生产一致：local-{prefix}-{8位hex}"""
    return f"local-{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def store():
    """连接真实 Postgres 的 store；连不上则跳过（容器未启动/DSN 未配置）"""
    if not _DSN:
        pytest.skip("未设置 POSTGRES_DSN：跳过 task_store 集成验证")
    store = PostgresTaskStore(dsn=_DSN)
    try:
        _run_async(store.init_schema())
    except Exception as e:
        pytest.skip(f"Postgres 不可用（{_DSN}），跳过 task_store 集成验证: {e}")
    yield store
    _run_async(store.close())


class TestSchemaAndRoundtrip:
    def test_init_schema_is_idempotent(self, store):
        # 连续两次建表不报错（API 进程与 worker 进程各调一次的真实场景）
        _run_async(store.init_schema())

    def test_create_and_get_roundtrip(self, store):
        task_key = _unique_key("rt")

        async def _run():
            job_id = await store.create_or_replace_task(
                task_key, "local", task_key[len("local-") :], "查询任务"
            )
            row = await store.get_task(task_key)
            return job_id, row

        job_id, row = _run_async(_run())
        assert job_id == f"task:{task_key}:1"
        assert row is not None
        assert row["task_key"] == task_key
        assert row["user_id"] == "local"
        assert row["query"] == "查询任务"
        assert row["status"] == "pending"
        assert row["submit_count"] == 1
        assert row["result"] is None
        assert row["error"] is None

    def test_get_unknown_task_returns_none(self, store):
        row = _run_async(store.get_task(_unique_key("missing")))
        assert row is None


class TestSubmitCountAndJobId:
    def test_resubmit_increments_submit_count_and_rebuilds_job_id(self, store):
        task_key = _unique_key("cnt")

        async def _run():
            first = await store.create_or_replace_task(
                task_key, "local", "t1", "第一轮"
            )
            second = await store.create_or_replace_task(task_key, "local", "t1", "第二轮")
            row = await store.get_task(task_key)
            return first, second, row

        first, second, row = _run_async(_run())
        assert first == f"task:{task_key}:1"
        assert second == f"task:{task_key}:2"
        assert row["submit_count"] == 2
        assert row["job_id"] == second
        assert row["query"] == "第二轮"
        assert row["status"] == "pending"

    def test_replace_running_task_resets_to_pending(self, store):
        """旧非终态行先置 cancelled 再起新一轮：新行必须是干净的 pending"""
        task_key = _unique_key("rep")

        async def _run():
            await store.create_or_replace_task(task_key, "local", "t1", "第一轮")
            await store.mark_running(task_key, "worker-a")
            await store.create_or_replace_task(task_key, "local", "t1", "第二轮")
            return await store.get_task(task_key)

        row = _run_async(_run())
        assert row["status"] == "pending"
        assert row["worker_id"] is None
        assert row["started_at"] is None
        assert row["finished_at"] is None


class TestStateMachineGuards:
    def test_mark_running_only_from_pending(self, store):
        task_key = _unique_key("run")

        async def _run():
            await store.create_or_replace_task(task_key, "local", "t1", "任务")
            ok = await store.mark_running(task_key, "worker-a")
            # 终态后不可再 running
            await store.mark_finished(task_key, "done", result="ok")
            again = await store.mark_running(task_key, "worker-b")
            return ok, again

        ok, again = _run_async(_run())
        assert ok is True
        assert again is False

    def test_mark_finished_cannot_overwrite_terminal_state(self, store):
        """WHERE 守卫：迟到的旧 worker 不能覆盖新提交/已终态的结果"""
        task_key = _unique_key("guard")

        async def _run():
            await store.create_or_replace_task(task_key, "local", "t1", "任务")
            first = await store.mark_finished(task_key, "done", result="v1")
            second = await store.mark_finished(task_key, "failed", error="late")
            return first, second, await store.get_task(task_key)

        first, second, row = _run_async(_run())
        assert first is True
        assert second is False
        assert row["status"] == "done"
        assert row["result"] == "v1"
        assert row["error"] is None

    def test_mark_finished_rejects_invalid_status(self, store):
        task_key = _unique_key("bad")
        with pytest.raises(ValueError):
            _run_async(store.mark_finished(task_key, "pending"))

    def test_mark_running_fails_after_cancel(self, store):
        task_key = _unique_key("rac")

        async def _run():
            await store.create_or_replace_task(task_key, "local", "t1", "任务")
            prev = await store.cancel_task(task_key)
            picked = await store.mark_running(task_key, "worker-a")
            return prev, picked

        prev, picked = _run_async(_run())
        # 入队后被取消：worker 领取必须失败（跳过执行的依据）
        assert prev == "pending"
        assert picked is False


class TestGenerationGuard:
    """
    代际守卫（QA 回归 P0-1）：mark_finished 携带 worker_id 时必须绑定任务
    代际，迟到的旧 worker 不得污染同会话替换后的新一轮任务
    """

    def test_stale_worker_cannot_mark_replaced_task_done(self, store):
        """场景 A：A running → 重提交 B → 旧 worker 落 done 必须被拒"""
        task_key = _unique_key("gen-a")

        async def _run():
            await store.create_or_replace_task(task_key, "local", "t1", "第一轮")
            await store.mark_running(task_key, "worker-old")
            # 用户重提交：create_or_replace 原子替换（cancel 旧行 + 新行 pending）
            await store.create_or_replace_task(task_key, "local", "t1", "第二轮")
            rejected = await store.mark_finished(
                task_key, "done", result="tokens_used=OLD_RUN", worker_id="worker-old"
            )
            return rejected, await store.get_task(task_key)

        rejected, row = _run_async(_run())
        assert rejected is False, "旧 worker 的 done 必须被代际守卫拒绝"
        # 新一轮任务未被污染：仍是干净的 pending
        assert row["status"] == "pending"
        assert row["submit_count"] == 2
        assert row["result"] is None

    def test_stale_worker_cannot_cancel_replaced_task(self, store):
        """场景 B：取消后宽限窗口内重提交，旧 worker 落 cancelled 必须被拒"""
        task_key = _unique_key("gen-b")

        async def _run():
            await store.create_or_replace_task(task_key, "local", "t1", "第一轮")
            await store.mark_running(task_key, "worker-old")
            await store.cancel_task(task_key)  # 用户取消（宽限窗口开始）
            # 宽限窗口内用户重提交
            await store.create_or_replace_task(task_key, "local", "t1", "第二轮")
            rejected = await store.mark_finished(
                task_key, "cancelled", worker_id="worker-old"
            )
            return rejected, await store.get_task(task_key)

        rejected, row = _run_async(_run())
        assert rejected is False, "新任务不得被旧 worker 的取消收尾杀掉"
        assert row["status"] == "pending"
        assert row["submit_count"] == 2

    def test_holding_worker_can_mark_finished(self, store):
        """正向路径：持有者（mark_running 写入的 worker_id）落终态不受影响"""
        task_key = _unique_key("gen-ok")

        async def _run():
            await store.create_or_replace_task(task_key, "local", "t1", "任务")
            await store.mark_running(task_key, "worker-me")
            ok = await store.mark_finished(
                task_key, "done", result="tokens_used=1", worker_id="worker-me"
            )
            return ok, await store.get_task(task_key)

        ok, row = _run_async(_run())
        assert ok is True
        assert row["status"] == "done"
        assert row["result"] == "tokens_used=1"

    def test_worker_id_mismatch_rejected_even_while_running(self, store):
        task_key = _unique_key("gen-mix")

        async def _run():
            await store.create_or_replace_task(task_key, "local", "t1", "任务")
            await store.mark_running(task_key, "worker-a")
            return await store.mark_finished(
                task_key, "done", worker_id="worker-impersonator"
            )

        assert _run_async(_run()) is False


class TestResetRunning:
    """崩溃恢复重置（QA 回归 P1-1）：running → pending 才能被重新领取"""

    def test_reset_running_clears_ownership(self, store):
        task_key = _unique_key("rst")

        async def _run():
            await store.create_or_replace_task(task_key, "local", "t1", "任务")
            await store.mark_running(task_key, "worker-crashed")
            reset = await store.reset_running(task_key)
            row = await store.get_task(task_key)
            repicked = await store.mark_running(task_key, "worker-new")
            return reset, row, repicked

        reset, row, repicked = _run_async(_run())
        assert reset is True
        assert row["status"] == "pending"
        assert row["worker_id"] is None
        assert row["started_at"] is None
        # 重置后新 worker 可正常领取
        assert repicked is True

    def test_reset_running_ignores_non_running(self, store):
        task_key = _unique_key("rst2")

        async def _run():
            await store.create_or_replace_task(task_key, "local", "t1", "任务")
            pending_reset = await store.reset_running(task_key)
            await store.mark_finished(task_key, "done")
            terminal_reset = await store.reset_running(task_key)
            return pending_reset, terminal_reset

        pending_reset, terminal_reset = _run_async(_run())
        assert pending_reset is False
        assert terminal_reset is False


class TestCancel:
    def test_cancel_returns_previous_status(self, store):
        task_key = _unique_key("cx")

        async def _run():
            await store.create_or_replace_task(task_key, "local", "t1", "任务")
            pending_prev = await store.cancel_task(task_key)
            # 终态后再取消：不可再取消
            terminal = await store.cancel_task(task_key)
            return pending_prev, terminal, await store.get_task(task_key)

        pending_prev, terminal, row = _run_async(_run())
        assert pending_prev == "pending"
        assert terminal is None
        assert row["status"] == "cancelled"
        assert row["finished_at"] is not None

    def test_cancel_running_returns_running_as_previous(self, store):
        task_key = _unique_key("cxr")

        async def _run():
            await store.create_or_replace_task(task_key, "local", "t1", "任务")
            await store.mark_running(task_key, "worker-a")
            return await store.cancel_task(task_key)

        assert _run_async(_run()) == "running"

    def test_cancel_unknown_returns_none(self, store):
        assert _run_async(store.cancel_task(_unique_key("none"))) is None


class TestListUnfinished:
    def test_lists_only_pending_and_running(self, store):
        key_a = _unique_key("lf")
        key_b = _unique_key("lf")
        key_c = _unique_key("lf")

        async def _run():
            await store.create_or_replace_task(key_a, "local", "ta", "任务A")
            await store.create_or_replace_task(key_b, "local", "tb", "任务B")
            await store.create_or_replace_task(key_c, "local", "tc", "任务C")
            await store.mark_finished(key_a, "done")
            await store.mark_running(key_b, "worker-a")
            rows = await store.list_unfinished()
            keys = {row["task_key"] for row in rows}
            return keys

        keys = _run_async(_run())
        assert key_a not in keys, "终态任务不得出现在 unfinished 列表"
        assert key_b in keys
        assert key_c in keys
