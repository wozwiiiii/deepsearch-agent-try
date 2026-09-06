"""
任务调度模式切换层测试（P0-2 阶段 1）

覆盖两条分支与四条关键路径：
1. inline 分支（默认）：提交/同 key 替换/取消（cancelled 与 cancelling 语义）/
   状态查询推断——行为与改造前 server.py 内联实现完全一致；
2. redis 分支：mock task_store 与 ARQ 连接池（不引入 fakeredis、不真连
   Redis），验证写表→入队的 job_id 约定、取消的 cancelling/cancelled/404
   三态、状态查询透传任务表字段；
3. WS 事件轮询桥：API 进程不经 monitor 直推（模拟 worker 进程经共享
   event_store 落库的事件），1s 轮询后经 WS 到达本连接。

不发真实 LLM 调用：inline 的 run_deep_agent 一律替换为可控协程。
"""

import asyncio
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api import server, task_service
from app.api.event_store import SqliteEventStore


def _wait_event(event: asyncio.Event, timeout_seconds: float = 1.0) -> None:
    """测试线程等待应用事件循环置位的 Event（create_task 的调度同步）"""
    deadline = time.monotonic() + timeout_seconds
    while not event.is_set() and time.monotonic() < deadline:
        time.sleep(0.02)


def _fresh_thread(prefix: str) -> str:
    """生成唯一 thread_id，避免用例间任务/事件串扰"""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def clear_inline_tasks():
    """每个用例前后清空 inline 进程内登记表，防止任务泄漏到后续用例"""
    task_service.active_tasks.clear()
    yield
    for task in list(task_service.active_tasks.values()):
        task.cancel()
    task_service.active_tasks.clear()


@pytest.fixture
def client(monkeypatch):
    """inline 模式客户端：TASK_QUEUE_MODE 强制 inline + Agent 替换为可控协程

    用 with 进入 lifespan 上下文：TestClient 的事件循环跨请求存活，
    后台 asyncio.Task 不会随单个请求结束被连带取消
    """
    monkeypatch.delenv("TASK_QUEUE_MODE", raising=False)
    task_service.TASK_QUEUE_MODE = "inline"
    with TestClient(server.app) as test_client:
        yield test_client


class TestInlineBranch:
    """inline 分支：行为与改造前 server.py 内联实现一致（回归守卫）"""

    def test_submit_returns_started_and_registers_task(self, client, monkeypatch):
        thread_id = _fresh_thread("sub")

        async def _brief_agent(query, thread_id_arg, user_id="local"):
            # 短暂挂起保证响应返回时任务仍在登记表内（done_callback 尚未触发）
            await asyncio.sleep(0.2)
            return 0

        monkeypatch.setattr(task_service, "run_deep_agent", _brief_agent)
        response = client.post(
            "/api/task", json={"query": "测试任务", "thread_id": thread_id}
        )
        assert response.status_code == 200
        assert response.json() == {"status": "started", "thread_id": thread_id}
        # 任务进入进程内登记表（task_service.active_tasks，原 server.active_tasks）
        task_key = f"local-{thread_id}"
        assert task_key in task_service.active_tasks

    def test_resubmit_same_key_replaces_running_task(self, client, monkeypatch):
        thread_id = _fresh_thread("replace")
        started = asyncio.Event()

        async def _slow_agent(query, thread_id_arg, user_id="local"):
            started.set()
            await asyncio.sleep(30)  # 足够长，保证第二次提交时仍在运行

        monkeypatch.setattr(task_service, "run_deep_agent", _slow_agent)
        task_key = f"local-{thread_id}"

        first = client.post(
            "/api/task", json={"query": "第一轮", "thread_id": thread_id}
        )
        assert first.status_code == 200
        # 等 _slow_agent 真正开始执行（create_task 调度到事件循环）
        _wait_event(started)
        old_task = task_service.active_tasks[task_key]
        assert not old_task.done()

        second = client.post(
            "/api/task", json={"query": "第二轮", "thread_id": thread_id}
        )
        assert second.status_code == 200
        new_task = task_service.active_tasks[task_key]
        assert new_task is not old_task, "同 key 重提交必须替换为新任务"
        # 旧任务被取消（对齐现有"同会话替换"语义）
        deadline = time.monotonic() + 2.0
        while not old_task.done() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert old_task.cancelled()

    def test_cancel_running_task_reports_cancelled_or_cancelling(
        self, client, monkeypatch
    ):
        thread_id = _fresh_thread("cancel")
        started = asyncio.Event()

        async def _slow_agent(query, thread_id_arg, user_id="local"):
            started.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(task_service, "run_deep_agent", _slow_agent)
        client.post("/api/task", json={"query": "长任务", "thread_id": thread_id})
        _wait_event(started)

        response = client.post(f"/api/task/{thread_id}/cancel")
        assert response.status_code == 200
        # 可取消的纯 sleep 协程 1 秒等待窗口内必然响应：cancelled
        assert response.json()["status"] in ("cancelled", "cancelling")

    def test_cancel_unknown_task_returns_404(self, client):
        thread_id = _fresh_thread("nope")
        response = client.post(f"/api/task/{thread_id}/cancel")
        assert response.status_code == 404
        assert "任务不存在或已结束" in response.json()["detail"]

    def test_status_running_task(self, client, monkeypatch):
        thread_id = _fresh_thread("status")
        started = asyncio.Event()

        async def _slow_agent(query, thread_id_arg, user_id="local"):
            started.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(task_service, "run_deep_agent", _slow_agent)
        client.post("/api/task", json={"query": "任务", "thread_id": thread_id})
        _wait_event(started)

        response = client.get(f"/api/task/{thread_id}/status")
        assert response.status_code == 200
        assert response.json()["status"] == "running"
        assert response.json()["thread_id"] == thread_id

    def test_status_unknown_task_returns_404(self, client):
        thread_id = _fresh_thread("nostatus")
        response = client.get(f"/api/task/{thread_id}/status")
        assert response.status_code == 404

    def test_status_requires_authentication(self, monkeypatch):
        # fail-closed：配置 API_KEYS 后无密钥访问状态接口必须 401
        monkeypatch.setenv("API_KEYS", "alice:sk-alice-0123456789abcdef")
        thread_id = _fresh_thread("authz")
        with TestClient(server.app) as c:
            response = c.get(f"/api/task/{thread_id}/status")
            assert response.status_code == 401


class _FakeArqPool:
    """ARQ 连接池替身：记录 enqueue_job 调用，不真连 Redis"""

    def __init__(self):
        self.enqueued: list[tuple] = []

    async def enqueue_job(self, job_name, *args, _job_id=None, **kwargs):
        self.enqueued.append((job_name, args, _job_id))


class _FakeTaskStore:
    """task_store 替身：内存表 + 提交计数，模拟任务表状态机"""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.next_submit_count: dict[str, int] = {}

    async def create_or_replace_task(self, task_key, user_id, thread_id, query):
        count = self.next_submit_count.get(task_key, 0) + 1
        self.next_submit_count[task_key] = count
        self.rows[task_key] = {
            "task_key": task_key,
            "user_id": user_id,
            "thread_id": thread_id,
            "query": query,
            "status": "pending",
            "submit_count": count,
            "worker_id": None,
            "error": None,
        }
        return f"task:{task_key}:{count}"

    async def cancel_task(self, task_key):
        row = self.rows.get(task_key)
        if row is None or row["status"] not in ("pending", "running"):
            return None
        prev = row["status"]
        row["status"] = "cancelled"
        return prev

    async def get_task(self, task_key):
        return self.rows.get(task_key)

    async def init_schema(self):
        # lifespan 启动钩子在 redis 模式会调用：替身吞掉即可
        pass

    async def close(self):
        pass


class TestRedisBranch:
    """redis 分支：mock task_store 与 ARQ 连接池，验证编排逻辑"""

    @pytest.fixture
    def redis_mode(self, monkeypatch):
        """切入 redis 模式并注入替身，结束后恢复 inline"""
        monkeypatch.delenv("TASK_QUEUE_MODE", raising=False)
        task_service.TASK_QUEUE_MODE = "redis"
        fake_store = _FakeTaskStore()
        fake_pool = _FakeArqPool()

        async def _fake_get_pool():
            return fake_pool

        monkeypatch.setattr(task_service, "task_store", fake_store)
        monkeypatch.setattr(task_service, "_get_arq_pool", _fake_get_pool)
        yield fake_store, fake_pool
        task_service.TASK_QUEUE_MODE = "inline"

    def test_submit_writes_table_and_enqueues_with_dedup_job_id(
        self, redis_mode, monkeypatch
    ):
        fake_store, fake_pool = redis_mode
        thread_id = _fresh_thread("rq")
        task_key = f"local-{thread_id}"

        with TestClient(server.app) as client:
            response = client.post(
                "/api/task", json={"query": "队列任务", "thread_id": thread_id}
            )
            assert response.status_code == 200
            assert response.json()["status"] == "started"

        # 写表：pending、submit_count=1
        row = fake_store.rows[task_key]
        assert row["status"] == "pending"
        assert row["submit_count"] == 1
        # 入队：job_id 遵循 f"task:{task_key}:{submit_count}" 约定（关键去重语义）
        assert len(fake_pool.enqueued) == 1
        job_name, args, job_id = fake_pool.enqueued[0]
        assert job_name == "run_task"
        assert args == (task_key,)
        assert job_id == f"task:{task_key}:1"

    def test_resubmit_increments_submit_count_and_rebuilds_job_id(
        self, redis_mode
    ):
        fake_store, fake_pool = redis_mode
        thread_id = _fresh_thread("rq2")
        task_key = f"local-{thread_id}"

        with TestClient(server.app) as client:
            client.post("/api/task", json={"query": "第一轮", "thread_id": thread_id})
            # 模拟 worker 已把第一轮置为 running（重提交必须替换非终态任务）
            fake_store.rows[task_key]["status"] = "running"
            client.post("/api/task", json={"query": "第二轮", "thread_id": thread_id})

        row = fake_store.rows[task_key]
        assert row["submit_count"] == 2
        assert row["status"] == "pending"
        _, _, job_id = fake_pool.enqueued[1]
        assert job_id == f"task:{task_key}:2"

    def test_cancel_running_reports_cancelling(self, redis_mode):
        fake_store, _ = redis_mode
        thread_id = _fresh_thread("rqc")
        task_key = f"local-{thread_id}"

        with TestClient(server.app) as client:
            client.post("/api/task", json={"query": "任务", "thread_id": thread_id})
            fake_store.rows[task_key]["status"] = "running"
            response = client.post(f"/api/task/{thread_id}/cancel")

        assert response.status_code == 200
        # 原 running → cancelling（worker 按轮询间隔中止，与 inline 语义一致）
        assert response.json()["status"] == "cancelling"
        assert fake_store.rows[task_key]["status"] == "cancelled"

    def test_cancel_unknown_returns_404(self, redis_mode):
        thread_id = _fresh_thread("rq404")
        with TestClient(server.app) as client:
            response = client.post(f"/api/task/{thread_id}/cancel")
        assert response.status_code == 404

    def test_status_reads_from_table(self, redis_mode):
        fake_store, _ = redis_mode
        thread_id = _fresh_thread("rqs")
        task_key = f"local-{thread_id}"

        with TestClient(server.app) as client:
            client.post("/api/task", json={"query": "任务", "thread_id": thread_id})
            fake_store.rows[task_key].update({"status": "done", "error": None})
            response = client.get(f"/api/task/{thread_id}/status")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "done"
        assert body["submit_count"] == 1

    def test_status_unknown_returns_404(self, redis_mode):
        thread_id = _fresh_thread("rqs404")
        with TestClient(server.app) as client:
            response = client.get(f"/api/task/{thread_id}/status")
        assert response.status_code == 404


class TestWsEventPollingBridge:
    """
    WS 事件轮询桥：模拟 worker 进程向共享 event_store 落库的事件
    （不经本进程 monitor 直推），验证 1s 轮询把新事件送达本连接
    """

    def test_bridge_delivers_worker_events(self, monkeypatch):
        thread_id = _fresh_thread("bridge")
        task_key = f"local-{thread_id}"
        # "worker 进程"使用独立连接写同一事件库（同机磁盘同路径），
        # 与生产部署的进程拓扑一致
        worker_store = SqliteEventStore()

        with TestClient(server.app) as client:
            with client.websocket_connect(f"/ws/{thread_id}") as ws:
                asyncio.run(
                    worker_store.append(task_key, "tool_start", "来自 worker 的事件")
                )
                worker_store_close = worker_store.close()

                # 轮询间隔 1s：收到事件前的心跳 pong 全部跳过，上限放宽到
                # 10 轮防偶发慢调度导致假失败；收到 monitor_event 即断言
                payload = None
                for _ in range(10):
                    message = ws.receive_json()
                    if message.get("type") == "monitor_event":
                        payload = message
                        break
                    ws.send_text("ping")
                asyncio.run(worker_store_close)

        assert payload is not None, "1s 轮询未在预算轮次内送达 worker 事件"
        assert payload["event"] == "tool_start"
        assert payload["message"] == "来自 worker 的事件"
        assert isinstance(payload["seq"], int)
