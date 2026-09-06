"""
事件回放测试（P0-3）

覆盖三层：
1. SqliteEventStore 单元测试：seq 递增、差量读取、流隔离、限长裁剪、重启可读；
2. monitor 集成：每条事件经 monitor 发出后先落库、payload 携带 seq；
3. WS 回放协议：last_seq 差量补发、首次连接恢复最近事件、非法 last_seq
   拒绝握手、实时事件携带 seq、跨租户事件不可回放。

测试事件库统一走 conftest 注入的临时目录 EVENT_DB；每个用例使用独立
task_key，避免同一测试会话内相互污染。
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import server
from app.api.context import (
    reset_session_context,
    set_session_context,
    set_thread_context,
)
from app.api.event_store import SqliteEventStore, event_store
from app.api.monitor import monitor


def _fresh_key(prefix: str) -> tuple[str, str]:
    """生成唯一的 (thread_id, 复合 task_key)，避免用例间事件串扰"""
    suffix = uuid.uuid4().hex[:8]
    thread_id = f"{prefix}-{suffix}"
    return thread_id, f"local-{thread_id}"


def _emit_and_wait(report_fn, task_key: str) -> None:
    """
    在测试主线程发出一条 monitor 事件并同步等待「落库+推送」完成。

    monitor 把协程投递到 app 事件循环执行，测试线程通过调度句柄等待，
    不需要轮询。
    """
    session_token = set_session_context("/tmp/event-replay-test")
    thread_token = set_thread_context(task_key)
    try:
        report_fn()
        handle = monitor._last_emit_handle
        assert handle is not None, "事件未被调度（事件循环未绑定？）"
        handle.result(timeout=2)
    finally:
        reset_session_context(session_token, thread_token)


class TestSqliteEventStore:
    """存储层单元测试：每个用例用独立实例 + 独立临时库"""

    def test_append_assigns_increasing_seq_and_payload_shape(self, tmp_path):
        store = SqliteEventStore(db_path=str(tmp_path / "ev.sqlite3"))

        async def _run():
            seqs = [
                await store.append("alice-t1", "tool_start", "开始执行工具: x", {"a": 1}),
                await store.append("alice-t1", "task_result", "任务执行完成", {}),
            ]
            assert seqs[1] > seqs[0], "seq 必须严格递增"
            events = await store.read_after("alice-t1")
            assert [e["seq"] for e in events] == seqs
            # payload 结构与 monitor 实时推送一致，且带回放标记
            first = events[0]
            assert first["type"] == "monitor_event"
            assert first["event"] == "tool_start"
            assert first["message"] == "开始执行工具: x"
            assert first["data"] == {"a": 1}
            assert first["replay"] is True
            await store.close()

        asyncio.run(_run())

    def test_read_after_returns_only_events_after_last_seq(self, tmp_path):
        store = SqliteEventStore(db_path=str(tmp_path / "ev.sqlite3"))

        async def _run():
            s1 = await store.append("alice-t2", "tool_start", "e1")
            await store.append("alice-t2", "tool_start", "e2")
            await store.append("alice-t2", "task_result", "e3")
            diff = await store.read_after("alice-t2", last_seq=s1)
            assert [e["message"] for e in diff] == ["e2", "e3"]
            await store.close()

        asyncio.run(_run())

    def test_explicit_last_seq_replays_full_diff_beyond_replay_limit(self, tmp_path):
        """审查修复 R-2 回归：显式 last_seq 的差量不受 replay_limit 截断"""
        store = SqliteEventStore(
            db_path=str(tmp_path / "ev.sqlite3"), replay_limit=2, max_per_stream=10
        )

        async def _run():
            first = await store.append("alice-t7", "tool_start", "e0")
            for i in range(1, 5):  # 差量 4 条 > replay_limit=2
                await store.append("alice-t7", "tool_start", f"e{i}")
            diff = await store.read_after("alice-t7", last_seq=first)
            # 全量补发（上限是 max_per_stream 裁剪边界，不是 replay_limit）
            assert [e["message"] for e in diff] == ["e1", "e2", "e3", "e4"]
            # 首次连接（无 last_seq）仍是最近 replay_limit 条
            recent = await store.read_after("alice-t7")
            assert [e["message"] for e in recent] == ["e3", "e4"]
            await store.close()

        asyncio.run(_run())

    def test_concurrent_appends_assign_unique_ordered_seqs(self, tmp_path):
        """
        并发写入的 seq 不重复且读回有序（审查回归守卫）。

        前端丢件检测依赖"同流内 seq 严格递增"；若 append 的串行化
        被破坏（锁失效/改实现），此用例会先于线上乱序暴露。
        """
        store = SqliteEventStore(db_path=str(tmp_path / "ev.sqlite3"))

        async def _run():
            seqs = await asyncio.gather(
                *[store.append("alice-t8", "tool_start", f"e{i}") for i in range(20)]
            )
            assert len(set(seqs)) == 20, "seq 必须唯一"
            events = await store.read_after("alice-t8", limit=100)
            read_seqs = [e["seq"] for e in events]
            assert read_seqs == sorted(read_seqs), "读回必须按 seq 升序"
            assert set(read_seqs) == set(seqs)
            await store.close()

        asyncio.run(_run())

    def test_streams_are_isolated_by_task_key(self, tmp_path):
        store = SqliteEventStore(db_path=str(tmp_path / "ev.sqlite3"))

        async def _run():
            await store.append("alice-t3", "tool_start", "alice 的事件")
            await store.append("bob-t3", "tool_start", "bob 的事件")
            alice_events = await store.read_after("alice-t3")
            assert len(alice_events) == 1
            assert alice_events[0]["message"] == "alice 的事件"
            await store.close()

        asyncio.run(_run())

    def test_trim_keeps_only_most_recent_events(self, tmp_path):
        # max_per_stream=3：写入 5 条后只保留最近 3 条（等价 XADD MAXLEN）
        store = SqliteEventStore(db_path=str(tmp_path / "ev.sqlite3"), max_per_stream=3)

        async def _run():
            for i in range(5):
                await store.append("alice-t4", "tool_start", f"e{i}")
            events = await store.read_after("alice-t4", limit=100)
            assert [e["message"] for e in events] == ["e2", "e3", "e4"]
            await store.close()

        asyncio.run(_run())

    def test_first_connect_replay_respects_limit(self, tmp_path):
        store = SqliteEventStore(db_path=str(tmp_path / "ev.sqlite3"), replay_limit=2)

        async def _run():
            for i in range(3):
                await store.append("alice-t5", "tool_start", f"e{i}")
            events = await store.read_after("alice-t5")  # 无 last_seq → 最近 limit 条
            assert [e["message"] for e in events] == ["e1", "e2"]
            await store.close()

        asyncio.run(_run())

    def test_events_survive_new_store_instance(self, tmp_path):
        """新实例读回旧实例写入的事件：等价服务重启后历史仍可回放"""
        db_path = str(tmp_path / "ev.sqlite3")

        async def _write():
            store = SqliteEventStore(db_path=db_path)
            await store.append("alice-t6", "task_result", "最终答案")
            await store.close()

        async def _read():
            store = SqliteEventStore(db_path=db_path)
            events = await store.read_after("alice-t6")
            assert len(events) == 1
            assert events[0]["message"] == "最终答案"
            await store.close()

        asyncio.run(_write())
        asyncio.run(_read())


class TestMonitorPersistence:
    """monitor 集成：事件经 monitor 发出后必须先落库、payload 携带 seq"""

    def test_monitor_event_persisted_with_seq(self):
        _, task_key = _fresh_key("mon")
        with TestClient(server.app):
            _emit_and_wait(lambda: monitor.report_tool("search"), task_key)

            # 通过全局 event_store 读回（与 WS 端点同一实例）
            events = asyncio.run(event_store.read_after(task_key))
            assert len(events) == 1
            assert events[0]["event"] == "tool_start"
            assert events[0]["message"] == "开始执行工具: search"
            assert isinstance(events[0]["seq"], int)


class TestWsReplayProtocol:
    """WS 回放协议：差量补发、首次连接恢复、实时事件带 seq、租户隔离"""

    def test_reconnect_with_last_seq_replays_only_diff(self):
        thread_id, task_key = _fresh_key("diff")
        with TestClient(server.app) as client:
            for message in ["e1", "e2", "e3"]:
                _emit_and_wait(
                    lambda m=message: monitor.report_error(m), task_key
                )

            first_seq = asyncio.run(event_store.read_after(task_key))[0]["seq"]

            with client.websocket_connect(
                f"/ws/{thread_id}?last_seq={first_seq}"
            ) as ws:
                replayed = [ws.receive_json(), ws.receive_json()]
                assert [e["message"] for e in replayed] == ["e2", "e3"]
                # 补发事件带回放标记，前端据此跳过跳号检测
                assert all(e["replay"] is True for e in replayed)
                assert replayed[0]["seq"] == first_seq + 1
                # 补发完毕后不再有剩余事件：心跳 pong 应是下一条消息
                ws.send_text("ping")
                assert ws.receive_json()["type"] == "pong"

    def test_first_connect_replays_recent_events(self):
        thread_id, task_key = _fresh_key("fresh")
        with TestClient(server.app) as client:
            for message in ["e1", "e2"]:
                _emit_and_wait(
                    lambda m=message: monitor.report_error(m), task_key
                )

            with client.websocket_connect(f"/ws/{thread_id}") as ws:
                events = [ws.receive_json(), ws.receive_json()]
                assert [e["message"] for e in events] == ["e1", "e2"]
                ws.send_text("ping")
                assert ws.receive_json()["type"] == "pong"

    def test_live_event_carries_seq(self):
        thread_id, task_key = _fresh_key("live")
        with TestClient(server.app) as client:
            with client.websocket_connect(f"/ws/{thread_id}") as ws:
                # 连接内实时推送的事件（非补发）必须带 seq，前端靠它检测丢件
                _emit_and_wait(
                    lambda: monitor.report_error("realtime"), task_key
                )
                payload = ws.receive_json()
                assert payload["message"] == "realtime"
                assert isinstance(payload["seq"], int)
                assert "replay" not in payload

    def test_invalid_last_seq_rejected(self):
        thread_id, _ = _fresh_key("badseq")
        with TestClient(server.app) as client:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(f"/ws/{thread_id}?last_seq=abc"):
                    pass

    def test_replay_isolated_between_tenants(self, monkeypatch):
        """B 租户连接相同 thread_id 时，拿不到 A 租户的事件回放"""
        thread_id, _ = _fresh_key("iso")
        alice_key = f"alice-{thread_id}"
        monkeypatch.setenv(
            "API_KEYS",
            "alice:sk-alice-0123456789abcdef,bob:sk-bob-0123456789abcdef",
        )
        with TestClient(server.app) as client:
            # 事件归属 alice（复合键 alice-{thread_id}）
            session_token = set_session_context("/tmp/event-replay-test")
            thread_token = set_thread_context(alice_key)
            monitor.report_tool("search")
            monitor._last_emit_handle.result(timeout=2)
            reset_session_context(session_token, thread_token)

            # bob 连接同一 thread_id（经 POST /api/token 换短时令牌，
            # api_key 查询参数旧入口已移除）：事件库中 bob-{thread_id} 无任何
            # 事件，第一条收到的消息应是心跳 pong 而不是 alice 的回放
            bob_token = client.post(
                "/api/token", headers={"X-API-Key": "sk-bob-0123456789abcdef"}
            ).json()["token"]
            with client.websocket_connect(
                f"/ws/{thread_id}?token={bob_token}"
            ) as ws:
                ws.send_text("ping")
                assert ws.receive_json()["type"] == "pong"
