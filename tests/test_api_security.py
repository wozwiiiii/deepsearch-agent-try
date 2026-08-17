"""
API 层安全测试

基于 FastAPI TestClient，覆盖：
1. thread_id 白名单校验（防路径穿越拼接会话目录）；
2. 上传接口的文件名清洗、扩展名白名单和大小上限；
3. 文件列表/下载的会话隔离与路径穿越拒绝；
4. CORS 白名单行为。

run_deep_agent 被 monkeypatch 为空协程，测试不会发起真实 LLM 调用。
"""

import io

import pytest
from fastapi.testclient import TestClient

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
    return TestClient(server.app)


class TestThreadIdValidation:
    @pytest.mark.parametrize(
        "bad_thread_id",
        [
            "../evil",
            "..\\evil",
            "session/../secret",
            "a b",
            "a/b",
            "a:b",
            "x" * 65,
            "id;rm -rf",
        ],
    )
    def test_task_rejects_malformed_thread_id(self, client, bad_thread_id):
        response = client.post("/api/task", json={"query": "测试任务", "thread_id": bad_thread_id})
        assert response.status_code == 400

    def test_task_empty_thread_id_auto_generates(self, client):
        # 空 thread_id 语义等同于未提供，由服务端生成 uuid4
        response = client.post("/api/task", json={"query": "测试任务", "thread_id": ""})
        assert response.status_code == 200
        assert response.json()["thread_id"]

    def test_task_accepts_uuid_thread_id(self, client):
        response = client.post(
            "/api/task", json={"query": "测试任务", "thread_id": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "started"

    def test_files_rejects_malformed_thread_id(self, client):
        response = client.get("/api/files", params={"thread_id": "../app"})
        assert response.status_code == 400

    def test_download_rejects_malformed_thread_id(self, client):
        response = client.get(
            "/api/download", params={"thread_id": "../../etc", "path": "x.md"}
        )
        assert response.status_code == 400

    def test_task_rejects_oversized_query(self, client):
        response = client.post(
            "/api/task", json={"query": "x" * 20_001, "thread_id": "valid-id"}
        )
        assert response.status_code == 422


class TestUploadSafety:
    def _upload(self, client, filename, content=b"hello", thread_id="tid_test"):
        return client.post(
            "/api/upload",
            data={"thread_id": thread_id},
            files={"files": (filename, io.BytesIO(content), "application/octet-stream")},
        )

    def test_traversal_filename_sanitized_into_session_dir(self, client):
        response = self._upload(client, "../../evil.txt")
        assert response.status_code == 200
        # 文件名被清洗为 basename，且落在当前租户（开发模式 local）目录内
        assert response.json()["files"] == ["evil.txt"]
        stored = server.updated_dir / "user_local" / "session_tid_test" / "evil.txt"
        assert stored.exists()
        assert not (server.updated_dir.parent.parent / "evil.txt").exists()

    def test_disallowed_extension_rejected(self, client):
        response = self._upload(client, "payload.exe", b"MZ...")
        assert response.status_code == 400

    def test_allowed_extension_accepted(self, client):
        response = self._upload(client, "报告.md", "# 标题".encode("utf-8"))
        assert response.status_code == 200

    def test_upload_requires_valid_thread_id(self, client):
        response = self._upload(client, "a.txt", thread_id="../escape")
        assert response.status_code == 400
        assert not (server.updated_dir / "session_..").exists()

    def test_oversized_file_rejected_and_cleaned(self, client, monkeypatch):
        monkeypatch.setattr(server, "MAX_UPLOAD_SIZE", 8)
        monkeypatch.setattr(server, "MAX_UPLOAD_SIZE_MB", 0)
        response = self._upload(client, "big.txt", b"x" * 1024)
        assert response.status_code == 413
        # 超限的半成品文件必须被清理
        assert not (
            server.updated_dir / "user_local" / "session_tid_test" / "big.txt"
        ).exists()

    def _upload_many(self, client, thread_id, file_list):
        return client.post(
            "/api/upload",
            data={"thread_id": thread_id},
            files=[("files", (name, io.BytesIO(content))) for name, content in file_list],
        )

    def test_too_many_files_rejected_before_write(self, client, monkeypatch):
        # 文件数上限在写盘前拒绝（审查修复 M-1）
        monkeypatch.setattr(server, "MAX_UPLOAD_FILES", 2)
        response = self._upload_many(
            client, "tid_many", [("a.md", b"x"), ("b.md", b"x"), ("c.md", b"x")]
        )
        assert response.status_code == 413

    def test_total_size_limit_rolls_back_saved_files(self, client, monkeypatch):
        # 单文件都合法、但总量超限：已写入的文件必须回滚（审查修复 M-1/L-2）
        monkeypatch.setattr(server, "MAX_UPLOAD_TOTAL_SIZE", 8)
        response = self._upload_many(
            client, "tid_total", [("first.md", b"12345"), ("second.md", b"12345")]
        )
        assert response.status_code == 413
        session_dir = server.updated_dir / "user_local" / "session_tid_total"
        assert not (session_dir / "first.md").exists()
        assert not (session_dir / "second.md").exists()

    def test_invalid_extension_rolls_back_previous_files(self, client):
        # 第 2 个文件类型非法时，第 1 个已落盘文件必须回滚（审查修复 L-2）
        response = self._upload_many(
            client, "tid_rb", [("ok.md", b"x"), ("bad.exe", b"x")]
        )
        assert response.status_code == 400
        assert not (server.updated_dir / "user_local" / "session_tid_rb" / "ok.md").exists()


class TestFileIsolation:
    def _prepare_session(self, name="iso_test", filename="secret.md"):
        # 开发模式（未配置 API_KEYS）下所有请求归属 local 租户
        session_dir = server.output_dir / "user_local" / f"session_{name}"
        session_dir.mkdir(parents=True, exist_ok=True)
        target = session_dir / filename
        target.write_text("机密内容", encoding="utf-8")
        return session_dir, target

    def test_list_files_returns_relative_paths(self, client):
        self._prepare_session()
        response = client.get("/api/files", params={"thread_id": "iso_test"})
        assert response.status_code == 200
        files = response.json()["files"]
        assert files[0]["path"] == "secret.md"
        assert files[0]["thread_id"] == "iso_test"

    def test_list_missing_session_returns_404(self, client):
        response = client.get("/api/files", params={"thread_id": "no_such_session"})
        assert response.status_code == 404

    def test_download_within_session_ok(self, client):
        self._prepare_session()
        response = client.get(
            "/api/download", params={"thread_id": "iso_test", "path": "secret.md"}
        )
        assert response.status_code == 200
        assert response.content == "机密内容".encode("utf-8")

    @pytest.mark.parametrize(
        "traversal_path",
        [
            "../secret.md",
            "../../.env",
            "sub/../../../app/agent/llm.py",
        ],
    )
    def test_download_traversal_rejected(self, client, traversal_path):
        self._prepare_session()
        response = client.get(
            "/api/download", params={"thread_id": "iso_test", "path": traversal_path}
        )
        assert response.status_code == 400

    def test_download_other_session_file_rejected(self, client):
        # 会话 A 尝试通过相对路径读到会话 B 的产物必须被拒绝
        self._prepare_session(name="session_b", filename="b_secret.md")
        session_a = server.output_dir / "user_local" / "session_session_a"
        session_a.mkdir(parents=True, exist_ok=True)
        response = client.get(
            "/api/download",
            params={"thread_id": "session_a", "path": "../session_b/b_secret.md"},
        )
        assert response.status_code == 400


class TestCorsPolicy:
    def test_allowed_origin_echoed(self, client):
        response = client.get(
            "/health", headers={"Origin": "http://localhost:5173"}
        )
        assert response.status_code == 200
        assert (
            response.headers.get("access-control-allow-origin")
            == "http://localhost:5173"
        )

    def test_unknown_origin_not_echoed(self, client):
        response = client.get(
            "/health", headers={"Origin": "http://evil.example.com"}
        )
        assert response.headers.get("access-control-allow-origin") != (
            "http://evil.example.com"
        )


class TestHealthEndpoint:
    def test_health_returns_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
