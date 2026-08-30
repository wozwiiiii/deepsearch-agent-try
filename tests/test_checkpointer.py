"""
checkpointer 持久化测试

核心验证目标：SQLite checkpointer 替换 InMemorySaver 后，会话状态在
「新实例」（等价于服务重启）之间仍然可读。测试不经过 LLM，直接对
saver 做写入/读取回环。
"""

import asyncio
from pathlib import Path

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.agent import main_agent as main_agent_module


def _roundtrip(db_path: Path, thread_id: str) -> str:
    """
    写入一条 checkpoint 并返回其 id；随后用全新 saver 实例（模拟进程重启）
    读取同一条 checkpoint，验证持久化生效
    """

    async def _run() -> str:
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

        # 第一个实例：写入
        async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
            checkpoint = empty_checkpoint()
            await saver.aput(config, checkpoint, {"source": "test"}, {})
            written_id = checkpoint["id"]

        # 第二个实例：模拟服务重启后重新打开同一数据库文件
        async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
            found = await saver.aget_tuple(config)

        assert found is not None, "重启后 checkpoint 丢失"
        assert found.checkpoint["id"] == written_id
        return written_id

    return asyncio.run(_run())


class TestSqliteCheckpointPersistence:
    def test_checkpoint_survives_new_saver_instance(self, tmp_path: Path):
        db = tmp_path / "checkpoints.sqlite3"
        _roundtrip(db, "alice-t-persist-1")
        assert db.exists()

    def test_threads_are_independent(self, tmp_path: Path):
        db = tmp_path / "checkpoints.sqlite3"
        _roundtrip(db, "alice-t-thread-1")
        # 不同 thread_id 之间互不可见，等价于租户/会话状态隔离
        _roundtrip(db, "bob-t-thread-1")

        async def _assert_missing():
            async with AsyncSqliteSaver.from_conn_string(str(db)) as saver:
                assert await saver.aget_tuple(
                    {"configurable": {"thread_id": "carol-t-thread-1", "checkpoint_ns": ""}}
                ) is None

        asyncio.run(_assert_missing())


class TestLazyAgentInit:
    def test_get_agent_creates_checkpoint_db_and_caches(self, tmp_path, monkeypatch):
        db_path = tmp_path / "cp.sqlite3"
        monkeypatch.setattr(main_agent_module, "CHECKPOINT_DB", str(db_path))
        monkeypatch.setattr(main_agent_module, "_main_agent", None)
        monkeypatch.setattr(main_agent_module, "_checkpoint_saver", None)

        agent = asyncio.run(main_agent_module._get_agent())
        assert agent is not None
        assert db_path.exists()

        # 二次调用复用同一实例，不重复建立数据库连接
        agent_again = asyncio.run(main_agent_module._get_agent())
        assert agent_again is agent


class TestModelCallLimitMiddleware:
    """第三批成本治理：Agent 必须带模型调用硬上限，超限抛错由上层统一捕获"""

    def test_agent_configured_with_call_limit_middleware(self, tmp_path, monkeypatch):
        from langchain.agents.middleware import ModelCallLimitMiddleware

        recorded = {}

        def _fake_create_deep_agent(**kwargs):
            recorded.update(kwargs)
            return object()

        monkeypatch.setattr(main_agent_module, "create_deep_agent", _fake_create_deep_agent)
        monkeypatch.setattr(
            main_agent_module, "CHECKPOINT_DB", str(tmp_path / "cp.sqlite3")
        )
        monkeypatch.setattr(main_agent_module, "_main_agent", None)
        monkeypatch.setattr(main_agent_module, "_checkpoint_saver", None)
        monkeypatch.setattr(main_agent_module, "MODEL_RUN_LIMIT", 5)
        monkeypatch.setattr(main_agent_module, "MODEL_THREAD_LIMIT", 9)

        asyncio.run(main_agent_module._get_agent())

        middlewares = recorded.get("middleware", [])
        assert len(middlewares) == 1
        assert isinstance(middlewares[0], ModelCallLimitMiddleware)
        # 上限值透传正确，超限行为为抛错（而不是静默截断）
        assert middlewares[0].run_limit == 5
        assert middlewares[0].thread_limit == 9


class _RecordingMonitor:
    """替身 monitor：记录 error 事件，其余上报静默（避免触碰真实 WS/事件库）"""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def report_session_dir(self, path: str) -> None:  # noqa: ARG002
        pass

    def report_error(self, message: str) -> None:
        self.errors.append(message)

    def report_task_cancelled(self) -> None:
        pass

    def report_task_result(self, result: str) -> None:  # noqa: ARG002
        pass

    def report_assistant(self, name: str, args=None) -> None:  # noqa: ARG002
        pass


class TestTaskTimeout:
    """P1-4 任务硬超时：执行流被超时取消并经 monitor 告知前端，而非永远运行中"""

    def test_timeout_cancels_execution_and_reports_error(self, tmp_path, monkeypatch):
        class _HangingAgent:
            """astream 永久挂起，模拟 LLM API 网络半开"""

            async def astream(self, *args, **kwargs):
                await asyncio.sleep(30)
                yield {}  # pragma: no cover - 不会执行到

        async def _fake_get_agent():
            return _HangingAgent()

        stub_monitor = _RecordingMonitor()
        monkeypatch.setattr(main_agent_module, "project_root_path", tmp_path)
        monkeypatch.setattr(main_agent_module, "TASK_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(main_agent_module, "_get_agent", _fake_get_agent)
        monkeypatch.setattr(main_agent_module, "monitor", stub_monitor)

        # 超时后 run_deep_agent 正常返回（不抛出），错误经 monitor 上报
        asyncio.run(main_agent_module.run_deep_agent("查询任务", "t-timeout-1", "local"))

        assert any("硬超时" in message for message in stub_monitor.errors), (
            f"应上报超时错误，实际收到: {stub_monitor.errors}"
        )
        # 超时也应有会话工作目录（目录创建发生在执行之前）
        assert (tmp_path / "output" / "user_local" / "session_t-timeout-1").exists()

    def test_timeout_covers_agent_initialization(self, tmp_path, monkeypatch):
        """Agent 惰性初始化挂起同样被超时覆盖（wait_for 包住整个执行）"""

        async def _hanging_get_agent():
            await asyncio.sleep(30)
            return object()  # pragma: no cover - 不会执行到

        stub_monitor = _RecordingMonitor()
        monkeypatch.setattr(main_agent_module, "project_root_path", tmp_path)
        monkeypatch.setattr(main_agent_module, "TASK_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(main_agent_module, "_get_agent", _hanging_get_agent)
        monkeypatch.setattr(main_agent_module, "monitor", stub_monitor)

        asyncio.run(main_agent_module.run_deep_agent("查询任务", "t-timeout-2", "local"))

        assert any("硬超时" in message for message in stub_monitor.errors)
