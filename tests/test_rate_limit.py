"""
限流测试（第三批改造）

覆盖：
1. 限流键构造：带密钥的请求按密钥哈希计，无密钥回退客户端 IP；
2. 超限行为：同一身份连续请求超过阈值后返回 429；
3. 限流与认证的顺序：未认证请求先被 401/503 拦截，不消耗限流额度。
"""

import hashlib

import pytest
from fastapi.testclient import TestClient
from slowapi.util import get_remote_address

from app.api import server


@pytest.fixture(autouse=True)
def isolate_runtime_dirs(tmp_path, monkeypatch):
    """运行时目录指向临时目录：测试不读写真实 output/updated（审查修复 M-2）"""
    monkeypatch.setattr(server, "output_dir", tmp_path / "output")
    monkeypatch.setattr(server, "updated_dir", tmp_path / "updated")


@pytest.fixture
def client(monkeypatch):
    async def _noop_agent(*args, **kwargs):
        return None

    monkeypatch.setattr(server, "run_deep_agent", _noop_agent)
    # 每个用例前清空限流器内存状态，避免用例间相互污染
    server.limiter.reset()
    yield TestClient(server.app)
    server.limiter.reset()


class TestRateLimitKey:
    def test_key_from_api_key_header_is_hashed(self):
        class _FakeRequest:
            headers = {"X-API-Key": "sk-alice-0123456789abcdef"}
            query_params = {}

        key = server._rate_limit_key(_FakeRequest())
        digest = hashlib.sha256(b"sk-alice-0123456789abcdef").hexdigest()[:16]
        assert key == f"key:{digest}"
        # 键中不得出现明文密钥
        assert "sk-alice" not in key

    def test_key_from_query_param_fallback(self):
        class _FakeRequest:
            headers = {}
            query_params = {"api_key": "sk-bob-0123456789abcdef"}

        key = server._rate_limit_key(_FakeRequest())
        assert key.startswith("key:")

    def test_key_falls_back_to_ip_without_key(self):
        class _FakeRequest:
            headers = {}
            query_params = {}
            # get_remote_address 读取 request.client.host
            client = type("Addr", (), {"host": "127.0.0.1", "port": 80})()

        key = server._rate_limit_key(_FakeRequest())
        # 无密钥（开发模式）按 IP 限流，键前缀区分两种身份来源
        assert key == "ip:127.0.0.1"


class TestRateLimitEnforcement:
    def test_task_exceeds_limit_returns_429(self, client, monkeypatch):
        monkeypatch.delenv("API_KEYS", raising=False)  # conftest 已开启开发模式
        monkeypatch.setattr(server, "RATE_LIMIT_TASK", "2/minute")

        payload = {"query": "测试任务", "thread_id": "t-rate-1"}
        first = client.post("/api/task", json=payload)
        second = client.post("/api/task", json=payload)
        third = client.post("/api/task", json=payload)

        assert first.status_code == 200
        assert second.status_code == 200
        assert third.status_code == 429
        assert "频繁" in third.json()["detail"]

    def test_upload_exceeds_limit_returns_429(self, client, monkeypatch):
        import io

        monkeypatch.delenv("API_KEYS", raising=False)
        monkeypatch.setattr(server, "RATE_LIMIT_UPLOAD", "1/minute")

        def _post():
            return client.post(
                "/api/upload",
                data={"thread_id": "t-rate-2"},
                files={"files": ("a.md", io.BytesIO(b"x"))},
            )

        assert _post().status_code == 200
        assert _post().status_code == 429

    def test_different_keys_have_independent_quotas(self, client, monkeypatch):
        monkeypatch.setenv("API_KEYS", "alice:sk-alice-0123456789abcdef,bob:sk-bob-0123456789abcdef")
        monkeypatch.setattr(server, "RATE_LIMIT_TASK", "1/minute")

        payload = {"query": "测试任务", "thread_id": "t-rate-3"}
        alice_headers = {"X-API-Key": "sk-alice-0123456789abcdef"}
        bob_headers = {"X-API-Key": "sk-bob-0123456789abcdef"}

        assert client.post("/api/task", json=payload, headers=alice_headers).status_code == 200
        assert client.post("/api/task", json=payload, headers=alice_headers).status_code == 429
        # bob 有独立配额，不受 alice 超限影响
        assert client.post("/api/task", json=payload, headers=bob_headers).status_code == 200


class TestAuthRunsBeforeRateLimit:
    def test_unauthenticated_request_returns_401_not_429(self, client, monkeypatch):
        monkeypatch.setenv("API_KEYS", "alice:sk-alice-0123456789abcdef")
        monkeypatch.setattr(server, "RATE_LIMIT_TASK", "1/minute")

        payload = {"query": "测试任务", "thread_id": "t-rate-4"}
        headers = {"X-API-Key": "sk-alice-0123456789abcdef"}

        # 第一次成功、第二次超限，属于同一密钥
        assert client.post("/api/task", json=payload, headers=headers).status_code == 200
        assert client.post("/api/task", json=payload, headers=headers).status_code == 429

        # 无密钥请求先被依赖注入的认证拦下（401），不消耗任何配额
        no_key = client.post("/api/task", json=payload)
        assert no_key.status_code == 401

        # 错误密钥同样走认证失败路径
        wrong_key = client.post("/api/task", json=payload, headers={"X-API-Key": "sk-nope"})
        assert wrong_key.status_code == 401
