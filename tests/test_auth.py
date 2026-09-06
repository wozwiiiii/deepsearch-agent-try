"""
API Key 认证与多租户隔离测试

覆盖：
1. API_KEYS 配置解析的合法/非法用例；
2. 未配置（本地开发模式）、正确密钥、错误/缺失密钥三类认证行为；
3. 会话目录按租户隔离：A 租户无法列举/下载 B 租户的产物；
4. WebSocket 握手鉴权。
"""

import io

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import server, task_service
from app.api.auth import DEV_USER_ID, authenticate_api_key, parse_api_keys


class TestParseApiKeys:
    def test_empty_config_returns_empty_dict(self):
        assert parse_api_keys("") == {}
        assert parse_api_keys(" , ") == {}

    def test_valid_pairs_parsed(self):
        keys = parse_api_keys("alice:sk-alice-0123456789abcdef, bob:sk-bob-0123456789abcdef")
        assert keys == {
            "sk-alice-0123456789abcdef": "alice",
            "sk-bob-0123456789abcdef": "bob",
        }

    @pytest.mark.parametrize(
        "raw",
        [
            "alice-without-separator",  # 缺冒号
            "Alice:sk-alice-0123456789abcdef",  # 用户名大写非法
            "alice-b:sk-0123456789abcdef",  # 用户名含连字符（会破坏复合键）
            "alice:short",  # 密钥过短
            "alice:sk-alice-0123456789abcdef,alice:sk-alice-0123456789abcdef",  # 重复密钥
        ],
    )
    def test_invalid_config_raises(self, raw):
        with pytest.raises(RuntimeError):
            parse_api_keys(raw)


class TestDevModeFailClosed:
    """第三批改造：未配置 API_KEYS 时默认拒绝（fail-closed），需显式 ALLOW_DEV_MODE=1"""

    def test_no_keys_no_dev_mode_returns_503(self, monkeypatch):
        monkeypatch.delenv("API_KEYS", raising=False)
        monkeypatch.delenv("ALLOW_DEV_MODE", raising=False)
        with pytest.raises(HTTPException) as exc_info:
            authenticate_api_key("whatever")
        assert exc_info.value.status_code == 503

    def test_no_keys_explicit_dev_mode_allowed(self, monkeypatch):
        monkeypatch.delenv("API_KEYS", raising=False)
        monkeypatch.setenv("ALLOW_DEV_MODE", "1")
        assert authenticate_api_key("whatever").user_id == DEV_USER_ID

    def test_dev_mode_not_1_rejected(self, monkeypatch):
        # 值不等于 "1" 一律拒绝，防止误配 true/yes 等"看起来像开"的值
        monkeypatch.delenv("API_KEYS", raising=False)
        monkeypatch.setenv("ALLOW_DEV_MODE", "true")
        with pytest.raises(HTTPException) as exc_info:
            authenticate_api_key("whatever")
        assert exc_info.value.status_code == 503

    def test_endpoint_503_without_keys_and_dev_mode(self, client, monkeypatch):
        monkeypatch.delenv("API_KEYS", raising=False)
        monkeypatch.delenv("ALLOW_DEV_MODE", raising=False)
        response = client.post(
            "/api/task", json={"query": "任务", "thread_id": "t-fail-1"}
        )
        assert response.status_code == 503
        assert "ALLOW_DEV_MODE" in response.json()["detail"]

    def test_ws_rejected_without_keys_and_dev_mode(self, client, monkeypatch):
        monkeypatch.delenv("API_KEYS", raising=False)
        monkeypatch.delenv("ALLOW_DEV_MODE", raising=False)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/t-fail-2"):
                pass


class TestAuthenticateApiKey:
    def test_dev_mode_when_not_configured(self, monkeypatch):
        monkeypatch.delenv("API_KEYS", raising=False)
        principal = authenticate_api_key("whatever")
        assert principal.user_id == DEV_USER_ID

    def test_valid_key_returns_tenant(self, monkeypatch):
        monkeypatch.setenv("API_KEYS", "alice:sk-alice-0123456789abcdef")
        principal = authenticate_api_key("sk-alice-0123456789abcdef")
        assert principal.user_id == "alice"

    @pytest.mark.parametrize("bad_key", [None, "", "sk-wrong-0123456789abcdef"])
    def test_missing_or_wrong_key_rejected(self, monkeypatch, bad_key):
        monkeypatch.setenv("API_KEYS", "alice:sk-alice-0123456789abcdef")
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            authenticate_api_key(bad_key)
        assert exc_info.value.status_code == 401

    def test_non_ascii_key_rejected_not_500(self, client, monkeypatch):
        # 非 ASCII 密钥头应返回 401，而不是 compare_digest 抛 TypeError 变成 500
        # （审查修复 L-1）。httpx 允许 bytes 形式的头值，UTF-8 字节经服务端
        # latin-1 解码后即为非 ASCII 字符串
        monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY}")
        response = client.post(
            "/api/task",
            json={"query": "任务", "thread_id": "t-ascii"},
            headers={"X-API-Key": "sk-密钥-1234567890abcdef".encode("utf-8")},
        )
        assert response.status_code == 401


@pytest.fixture(autouse=True)
def isolate_runtime_dirs(tmp_path, monkeypatch):
    """运行时目录指向临时目录：测试不读写真实 output/updated（审查修复 M-2）"""
    monkeypatch.setattr(server, "output_dir", tmp_path / "output")
    monkeypatch.setattr(server, "updated_dir", tmp_path / "updated")


@pytest.fixture
def client(monkeypatch):
    async def _noop_agent(*args, **kwargs):
        return None

    # P0-2 阶段 1：inline 执行逻辑迁入 task_service，patch 点随迁
    monkeypatch.setattr(task_service, "run_deep_agent", _noop_agent)
    return TestClient(server.app)


ALICE_KEY = "sk-alice-0123456789abcdef"
BOB_KEY = "sk-bob-0123456789abcdef"
ALICE_HEADERS = {"X-API-Key": ALICE_KEY}
BOB_HEADERS = {"X-API-Key": BOB_KEY}


class TestEndpointAuth:
    def test_configured_keys_require_authentication(self, client, monkeypatch):
        monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY},bob:{BOB_KEY}")

        no_key = client.post("/api/task", json={"query": "任务", "thread_id": "t-auth-1"})
        assert no_key.status_code == 401

        wrong_key = client.post(
            "/api/task", json={"query": "任务", "thread_id": "t-auth-1"}, headers={"X-API-Key": "sk-nope"}
        )
        assert wrong_key.status_code == 401

        ok = client.post(
            "/api/task", json={"query": "任务", "thread_id": "t-auth-1"}, headers=ALICE_HEADERS
        )
        assert ok.status_code == 200

    def test_unauthenticated_upload_rejected_when_configured(self, client, monkeypatch):
        monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY}")
        response = client.post("/api/upload", data={"thread_id": "t-auth-2"}, files={"files": ("a.md", io.BytesIO(b"x"))})
        assert response.status_code == 401

    def test_misconfigured_keys_surface_as_500(self, client, monkeypatch):
        monkeypatch.setenv("API_KEYS", "bad-format-no-colon")
        response = client.post("/api/task", json={"query": "任务", "thread_id": "t-auth-3"})
        assert response.status_code == 500

    def test_health_needs_no_auth(self, client, monkeypatch):
        monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY}")
        assert client.get("/health").status_code == 200


class TestTenantIsolation:
    def _prepare_file(self, thread_id, filename="secret.md", content="alice 的机密"):
        session_dir = server.output_dir / "user_alice" / f"session_{thread_id}"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / filename).write_text(content, encoding="utf-8")

    def test_upload_lands_in_tenant_directory(self, client, monkeypatch):
        monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY}")
        response = client.post(
            "/api/upload",
            data={"thread_id": "t-iso-up"},
            files={"files": ("报告.md", io.BytesIO("# x".encode("utf-8")))},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        assert (server.updated_dir / "user_alice" / "session_t-iso-up" / "报告.md").exists()
        assert not (server.updated_dir / "user_local" / "session_t-iso-up").exists()

    def test_same_thread_id_isolated_between_tenants(self, client, monkeypatch):
        monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY},bob:{BOB_KEY}")
        self._prepare_file("shared-tid")

        alice_list = client.get("/api/files", params={"thread_id": "shared-tid"}, headers=ALICE_HEADERS)
        assert alice_list.status_code == 200
        assert alice_list.json()["files"][0]["path"] == "secret.md"

        # bob 使用相同 thread_id，只能看到自己（不存在的）会话目录
        bob_list = client.get("/api/files", params={"thread_id": "shared-tid"}, headers=BOB_HEADERS)
        assert bob_list.status_code == 404

        bob_download = client.get(
            "/api/download",
            params={"thread_id": "shared-tid", "path": "secret.md"},
            headers=BOB_HEADERS,
        )
        assert bob_download.status_code == 404

    def test_download_via_query_param_auth_removed(self, client, monkeypatch):
        """api_key 查询参数旧入口已移除：下载接口不再接受查询参数密钥（401）"""
        monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY}")
        self._prepare_file("t-iso-dl")
        response = client.get(
            "/api/download",
            params={"thread_id": "t-iso-dl", "path": "secret.md", "api_key": ALICE_KEY},
        )
        assert response.status_code == 401


class TestWebSocketAuth:
    def test_ws_rejected_without_key_when_configured(self, client, monkeypatch):
        monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY}")
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/t-ws-1"):
                pass

    def test_ws_api_key_query_param_rejected(self, client, monkeypatch):
        """api_key 查询参数旧入口已移除：携带它连接应 fail-closed 被拒"""
        monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY}")
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/ws/t-ws-2?api_key={ALICE_KEY}"):
                pass

    def test_ws_malformed_thread_id_rejected(self, client, monkeypatch):
        monkeypatch.delenv("API_KEYS", raising=False)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/../escape"):
                pass


class TestLinkTokens:
    """P1-2 短时链接令牌：签发需头认证、回环鉴权、过期/伪造拒绝、租户归属"""

    def test_token_endpoint_requires_header_auth(self, client, monkeypatch):
        monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY}")
        # 无凭据 → 401；错误密钥 → 401（签发接口不开放查询参数认证）
        assert client.post("/api/token").status_code == 401
        assert client.post("/api/token", headers={"X-API-Key": "sk-nope"}).status_code == 401

    def test_token_roundtrip_via_ws(self, client, monkeypatch):
        monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY}")
        response = client.post("/api/token", headers=ALICE_HEADERS)
        assert response.status_code == 200
        body = response.json()
        assert body["expires_in"] > 0
        # 令牌换取的 WS 握手与 api_key 等价
        with client.websocket_connect(f"/ws/t-tok-1?token={body['token']}") as ws:
            ws.send_text("ping")
            assert ws.receive_json()["type"] == "pong"

    def test_token_roundtrip_via_download(self, client, monkeypatch):
        monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY}")
        TestTenantIsolation()._prepare_file("t-tok-dl")
        token = client.post("/api/token", headers=ALICE_HEADERS).json()["token"]
        response = client.get(
            "/api/download",
            params={"thread_id": "t-tok-dl", "path": "secret.md", "token": token},
        )
        assert response.status_code == 200

    def test_token_binds_to_issuing_tenant(self, client, monkeypatch):
        """令牌归属签发者：bob 的有效令牌也只定位到 bob 自己的目录（隔离不因令牌失效）"""
        monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY},bob:{BOB_KEY}")
        TestTenantIsolation()._prepare_file("t-tok-iso")
        alice_token = client.post("/api/token", headers=ALICE_HEADERS).json()["token"]
        # alice 的令牌归属 alice：可下载自己的文件
        ok = client.get(
            "/api/download",
            params={"thread_id": "t-tok-iso", "path": "secret.md", "token": alice_token},
        )
        assert ok.status_code == 200
        # bob 的令牌同样有效，但同一 thread_id 只会定位到 bob 的目录（不存在 → 404）
        bob_token = client.post("/api/token", headers=BOB_HEADERS).json()["token"]
        bob_via_own = client.get(
            "/api/download",
            params={"thread_id": "t-tok-iso", "path": "secret.md", "token": bob_token},
        )
        assert bob_via_own.status_code == 404

    def test_expired_token_rejected(self, client, monkeypatch):
        monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY}")
        # TTL=0：签发即过期（issue_link_token 每次调用重读环境变量）
        monkeypatch.setenv("LINK_TOKEN_TTL_SECONDS", "0")
        token = client.post("/api/token", headers=ALICE_HEADERS).json()["token"]
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/ws/t-tok-exp?token={token}"):
                pass

    def test_garbage_token_rejected(self, client, monkeypatch):
        monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY}")
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/t-tok-bad?token=forged-token-value"):
                pass

    def test_api_key_query_param_removed(self, client, monkeypatch):
        """兼容期已结束：api_key 查询参数不再被接受（fail-closed 拒绝握手）"""
        monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY}")
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/ws/t-tok-compat?api_key={ALICE_KEY}"):
                pass
