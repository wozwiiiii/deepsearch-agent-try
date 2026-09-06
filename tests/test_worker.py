"""
ARQ worker 测试（P0-2 阶段 1）

不真起 ARQ worker、不真连 Redis：task_store 与 run_deep_agent 全部替换为
替身，直接调用任务函数 run_task 与启动钩子 recover_unfinished，验证：

1. 状态非 pending（入队后被取消/替换）→ 跳过执行；
2. 正常完成 → mark_finished('done')，返回值落表；
3. 执行异常 → mark_finished('failed', error=...)，不自动重试；
4. 轮询取消：取消观察协程先完成 → 取消执行协程 → 等清理分支跑完 →
   mark_finished('cancelled')；
5. 启动重拾：list_unfinished 逐行按 (task_key, submit_count) 确定性重建
   job_id 重入队，先 init_schema。
"""

import asyncio

import pytest

from app.queue import worker as worker_module
from app.queue.worker import job_id_for, recover_unfinished, run_task


class _FakeTaskStore:
    """task_store 替身：按预设脚本返回行，记录全部状态迁移"""

    def __init__(self, rows: dict[str, dict]):
        self.rows = rows
        self.mark_running_calls: list[tuple[str, str]] = []
        self.mark_finished_calls: list[tuple] = []
        self.reset_running_calls: list[str] = []
        self.init_schema_calls = 0
        # mark_running 的返回值可被用例改写（默认领取成功）
        self.mark_running_result = True
        # mark_finished 的返回值可被用例改写（模拟代际守卫拒绝）
        self.mark_finished_result = True

    async def get_task(self, task_key):
        return self.rows.get(task_key)

    async def mark_running(self, task_key, worker_id):
        self.mark_running_calls.append((task_key, worker_id))
        # 与真实 DAO 语义一致：领取成功时行迁移为 running 并写入代际身份
        row = self.rows.get(task_key)
        if (
            self.mark_running_result
            and row is not None
            and row["status"] == "pending"
        ):
            row["status"] = "running"
            row["worker_id"] = worker_id
        return self.mark_running_result

    async def mark_finished(
        self, task_key, status, result=None, error=None, worker_id=None
    ):
        self.mark_finished_calls.append((task_key, status, result, error, worker_id))
        return self.mark_finished_result

    async def reset_running(self, task_key):
        self.reset_running_calls.append(task_key)
        return True

    async def init_schema(self):
        self.init_schema_calls += 1

    async def list_unfinished(self):
        return [
            row
            for row in self.rows.values()
            if row["status"] in ("pending", "running")
        ]


class _FakeRedis:
    """ARQ ctx["redis"] 替身：记录 enqueue_job 调用"""

    def __init__(self):
        self.enqueued: list[tuple] = []

    async def enqueue_job(self, job_name, *args, _job_id=None, **kwargs):
        self.enqueued.append((job_name, args, _job_id))


def _pending_row(task_key: str) -> dict:
    return {
        "task_key": task_key,
        "user_id": "local",
        "thread_id": task_key.removeprefix("local-"),
        "query": "查询任务",
        "status": "pending",
        "submit_count": 1,
        "worker_id": None,
        "error": None,
    }


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    """把取消轮询间隔调小：取消路径用例毫秒级完成，不拖慢测试会话"""
    monkeypatch.setattr(worker_module, "CANCEL_POLL_SECONDS", 0.01)


@pytest.fixture(autouse=True)
def isolated_agent(monkeypatch):
    """保险丝：替身漏接时也不能发起真实 LLM 调用"""
    async def _forbidden_agent(*args, **kwargs):  # pragma: no cover
        raise AssertionError("测试路径不应触达真实 run_deep_agent")

    monkeypatch.setattr(worker_module, "run_deep_agent", _forbidden_agent)


class TestRunTask:
    def test_skips_non_pending_task(self, monkeypatch):
        """入队后被取消（状态非 pending）：跳过执行，不写任何终态"""
        task_key = "local-skip-1"
        store = _FakeTaskStore({task_key: {**_pending_row(task_key), "status": "cancelled"}})
        monkeypatch.setattr(worker_module, "task_store", store)

        result = asyncio.run(run_task({}, task_key))

        assert result == f"skipped:{task_key}"
        assert store.mark_running_calls == []
        assert store.mark_finished_calls == []

    def test_skips_unknown_task(self, monkeypatch):
        store = _FakeTaskStore({})
        monkeypatch.setattr(worker_module, "task_store", store)
        result = asyncio.run(run_task({}, "local-ghost"))
        assert result == "skipped:local-ghost"

    def test_completes_and_marks_done(self, monkeypatch):
        task_key = "local-done-1"
        store = _FakeTaskStore({task_key: _pending_row(task_key)})
        monkeypatch.setattr(worker_module, "task_store", store)

        async def _agent(query, thread_id, user_id="local"):
            assert query == "查询任务"
            return 1234

        monkeypatch.setattr(worker_module, "run_deep_agent", _agent)
        result = asyncio.run(run_task({}, task_key))

        assert result == f"done:{task_key}"
        assert len(store.mark_running_calls) == 1
        # 领取时的 worker_id 形如 host:pid:8位hex
        worker_id = store.mark_running_calls[0][1]
        assert len(worker_id.split(":")) == 3
        # run_deep_agent 返回的是 token 消耗，落 result 字段；
        # 落终态必须携带 worker_id 代际身份（QA 回归 P0-1）
        assert store.mark_finished_calls == [
            (task_key, "done", "tokens_used=1234", None, worker_id)
        ]

    def test_stale_done_rejected_by_generation_guard(self, monkeypatch):
        """执行期间任务被替换：mark_finished 被代际守卫拒绝 → 结果作废"""
        task_key = "local-stale-1"
        store = _FakeTaskStore({task_key: _pending_row(task_key)})
        store.mark_finished_result = False  # 模拟行已被新一轮提交接管
        monkeypatch.setattr(worker_module, "task_store", store)

        async def _agent(query, thread_id, user_id="local"):
            return 99

        monkeypatch.setattr(worker_module, "run_deep_agent", _agent)
        result = asyncio.run(run_task({}, task_key))

        assert result == f"stale:{task_key}"
        # 落终态尝试发生了，但返回值如实反映被拒
        assert store.mark_finished_calls[0][1] == "done"

    def test_marks_running_only_from_pending_guard(self, monkeypatch):
        """mark_running 竞态失败（已被取消/替换）：跳过且不落终态"""
        task_key = "local-race-1"
        store = _FakeTaskStore({task_key: _pending_row(task_key)})
        store.mark_running_result = False
        monkeypatch.setattr(worker_module, "task_store", store)

        result = asyncio.run(run_task({}, task_key))

        assert result == f"skipped:{task_key}"
        assert store.mark_finished_calls == []

    def test_failure_marks_failed_without_retry(self, monkeypatch):
        task_key = "local-fail-1"
        store = _FakeTaskStore({task_key: _pending_row(task_key)})
        monkeypatch.setattr(worker_module, "task_store", store)

        async def _failing_agent(query, thread_id, user_id="local"):
            raise RuntimeError("模型调用失败")

        monkeypatch.setattr(worker_module, "run_deep_agent", _failing_agent)
        result = asyncio.run(run_task({}, task_key))

        worker_id = store.mark_running_calls[0][1]
        assert result == f"failed:{task_key}"
        assert store.mark_finished_calls == [
            (task_key, "failed", None, "模型调用失败", worker_id)
        ]


class TestCancellation:
    def test_watch_cancellation_detects_cancelled_status(self, monkeypatch):
        """取消观察协程轮询到 cancelled 即返回（asyncio.wait 语义的基础）"""
        task_key = "local-watch-1"
        store = _FakeTaskStore(
            {
                task_key: {
                    **_pending_row(task_key),
                    "status": "running",
                    "worker_id": "worker-a",
                }
            }
        )

        async def _flip():
            await asyncio.sleep(0.03)
            store.rows[task_key]["status"] = "cancelled"

        async def _main():
            flip = asyncio.create_task(_flip())
            reason = await asyncio.wait_for(
                worker_module._watch_cancellation(task_key, "worker-a"), 2.0
            )
            await flip
            return reason

        monkeypatch.setattr(worker_module, "task_store", store)
        assert asyncio.run(_main()) == "cancelled"

    def test_watch_cancellation_detects_replacement(self, monkeypatch):
        """QA 回归 P0-1 场景 A：替换后行回到 pending，观察协程必须能发现"""
        task_key = "local-watch-2"
        store = _FakeTaskStore(
            {
                task_key: {
                    **_pending_row(task_key),
                    "status": "running",
                    "worker_id": "worker-old",
                }
            }
        )

        async def _flip():
            # 模拟 create_or_replace 原子替换：cancel 旧行 + 新行 pending
            # 在同一事务内提交，观察协程看不到 cancelled 窗口
            await asyncio.sleep(0.03)
            store.rows[task_key].update(
                {"status": "pending", "worker_id": None, "submit_count": 2}
            )

        async def _main():
            flip = asyncio.create_task(_flip())
            reason = await asyncio.wait_for(
                worker_module._watch_cancellation(task_key, "worker-old"), 2.0
            )
            await flip
            return reason

        monkeypatch.setattr(worker_module, "task_store", store)
        assert asyncio.run(_main()) == "replaced"

    def test_watch_cancellation_detects_new_worker_takeover(self, monkeypatch):
        """running 但 worker_id 换人：替换后已被新 worker 领取，本 worker 放弃"""
        task_key = "local-watch-3"
        store = _FakeTaskStore(
            {
                task_key: {
                    **_pending_row(task_key),
                    "status": "running",
                    "worker_id": "worker-new",
                }
            }
        )
        monkeypatch.setattr(worker_module, "task_store", store)
        reason = asyncio.run(
            asyncio.wait_for(
                worker_module._watch_cancellation(task_key, "worker-old"), 2.0
            )
        )
        assert reason == "replaced"

    def test_cancelled_midrun_marks_cancelled(self, monkeypatch):
        """执行中取消：观察协程先完成 → 取消执行 → 等清理分支 → 落 cancelled"""
        task_key = "local-cancel-1"
        store = _FakeTaskStore({task_key: _pending_row(task_key)})
        monkeypatch.setattr(worker_module, "task_store", store)
        cleanup_ran = asyncio.Event()

        async def _slow_agent(query, thread_id, user_id="local"):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                # 模拟 run_deep_agent 的 CancelledError 清理分支
                # （monitor 上报 task_cancelled、恢复 ContextVar）
                cleanup_ran.set()
                raise

        monkeypatch.setattr(worker_module, "run_deep_agent", _slow_agent)

        async def _main():
            # 模拟用户取消：50ms 后任务表状态翻转为 cancelled
            async def _flip():
                await asyncio.sleep(0.05)
                store.rows[task_key]["status"] = "cancelled"

            flip_task = asyncio.create_task(_flip())
            result = await run_task({}, task_key)
            await flip_task
            return result

        result = asyncio.run(_main())

        assert result == f"cancelled:{task_key}"
        assert cleanup_ran.is_set(), "必须等 run_deep_agent 的清理分支跑完"
        worker_id = store.mark_running_calls[0][1]
        assert store.mark_finished_calls == [
            (task_key, "cancelled", None, None, worker_id)
        ]

    def test_replaced_midrun_aborts_without_polluting_new_row(self, monkeypatch):
        """QA 回归 P0-1 场景 A 端到端：执行中被替换 → 中止旧 run 且不落终态"""
        task_key = "local-replace-1"
        store = _FakeTaskStore({task_key: _pending_row(task_key)})
        # 模拟代际守卫：行已被新一轮接管（worker_id 为 NULL）→ 拒绝旧 worker 落终态
        store.mark_finished_result = False
        monkeypatch.setattr(worker_module, "task_store", store)
        cleanup_ran = asyncio.Event()

        async def _slow_agent(query, thread_id, user_id="local"):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cleanup_ran.set()
                raise

        async def _main():
            # 50ms 后模拟 create_or_replace 原子替换：行直接变为新一轮 pending
            async def _flip():
                await asyncio.sleep(0.05)
                store.rows[task_key].update(
                    {"status": "pending", "worker_id": None, "submit_count": 2}
                )

            flip_task = asyncio.create_task(_flip())
            result = await run_task({}, task_key)
            await flip_task
            return result

        monkeypatch.setattr(worker_module, "run_deep_agent", _slow_agent)
        result = asyncio.run(_main())

        assert result == f"replaced:{task_key}"
        assert cleanup_ran.is_set(), "被替换也必须等旧 run 清理分支跑完"
        # 旧 worker 尝试落终态但被代际守卫拒绝（新行未被污染，无 done 写入）
        assert store.mark_finished_calls[0][1] == "cancelled"
        assert all(call[1] != "done" for call in store.mark_finished_calls)


class TestRecoverUnfinished:
    def test_requeues_unfinished_with_deterministic_job_id(self, monkeypatch):
        """启动重拾：先建表，再按 (task_key, submit_count) 重建 job_id 逐行入队"""
        key_pending = "local-p-1"
        key_running = "local-r-1"
        key_done = "local-d-1"
        store = _FakeTaskStore(
            {
                key_pending: {**_pending_row(key_pending), "submit_count": 3},
                key_running: _pending_row(key_running),
                key_done: {**_pending_row(key_done), "status": "done"},
            }
        )
        monkeypatch.setattr(worker_module, "task_store", store)
        redis = _FakeRedis()

        asyncio.run(recover_unfinished({"redis": redis}))

        assert store.init_schema_calls == 1
        assert len(redis.enqueued) == 2  # done 任务不重拾
        # 确定性 job_id：与提交时的 job_id 一致，ARQ 去重防重复执行
        assert redis.enqueued[0] == ("run_task", (key_pending,), f"task:{key_pending}:3")
        assert redis.enqueued[1] == (
            "run_task",
            (key_running,),
            job_id_for(key_running, 1),
        )

    def test_recovered_running_row_reset_to_pending(self, monkeypatch):
        """QA 回归 P1-1：running 行重拾前必须重置为 pending，否则永远被 skip"""
        key_running = "local-rr-1"
        store = _FakeTaskStore(
            {key_running: {**_pending_row(key_running), "status": "running"}}
        )
        monkeypatch.setattr(worker_module, "task_store", store)
        redis = _FakeRedis()

        asyncio.run(recover_unfinished({"redis": redis}))

        assert store.reset_running_calls == [key_running]
        # 重置后仍要入队（job_id 与提交时一致）
        assert redis.enqueued == [("run_task", (key_running,), f"task:{key_running}:1")]

    def test_recovered_running_task_can_be_rerun(self, monkeypatch):
        """重拾闭环：running → reset → pending → 新 worker 领取执行成功落 done"""
        task_key = "local-rr-2"
        store = _FakeTaskStore(
            {task_key: {**_pending_row(task_key), "status": "running"}}
        )
        monkeypatch.setattr(worker_module, "task_store", store)

        # 模拟 recover：reset_running 落到替身行上
        async def _fake_reset_running(key):
            store.reset_running_calls.append(key)
            row = store.rows[key]
            if row["status"] == "running":
                row.update({"status": "pending", "worker_id": None})
                return True
            return False

        monkeypatch.setattr(store, "reset_running", _fake_reset_running)
        asyncio.run(recover_unfinished({"redis": _FakeRedis()}))

        async def _agent(query, thread_id, user_id="local"):
            return 7

        monkeypatch.setattr(worker_module, "run_deep_agent", _agent)
        result = asyncio.run(run_task({}, task_key))
        assert result == f"done:{task_key}"
        assert store.mark_finished_calls[0][1] == "done"

    def test_recover_with_no_unfinished_tasks(self, monkeypatch):
        store = _FakeTaskStore({})
        monkeypatch.setattr(worker_module, "task_store", store)
        redis = _FakeRedis()

        asyncio.run(recover_unfinished({"redis": redis}))

        assert store.init_schema_calls == 1
        assert redis.enqueued == []
        assert store.reset_running_calls == []


class TestJobIdConvention:
    def test_job_id_format(self):
        assert job_id_for("local-thread-1", 7) == "task:local-thread-1:7"

    def test_worker_settings_wiring(self):
        """不自动重试 + 硬超时兜底 + 并发上限：配置接线回归"""
        settings = worker_module.WorkerSettings
        assert [f.__name__ for f in settings.functions] == ["run_task"]
        assert settings.max_tries == 1
        assert settings.job_timeout == 660
        assert settings.max_jobs == worker_module.WORKER_MAX_JOBS
        assert settings.queue_name == "deepsearch:tasks"
        assert settings.on_startup is recover_unfinished
