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
