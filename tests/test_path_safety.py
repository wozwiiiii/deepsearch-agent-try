"""
路径安全测试

覆盖 resolve_path 的核心安全契约：
1. 正常相对路径、虚拟前缀、updated 前缀都能正确落到会话目录内；
2. 路径穿越（../）、绝对路径（盘符 / Unix 根路径）一律抛 PathEscapeError；
3. 模型误拼的冗余目录层级会被压平，不影响正常使用。
"""

from pathlib import Path

import pytest

from app.utils.path_utils import PathEscapeError, resolve_path


@pytest.fixture
def session_dir(tmp_path: Path) -> str:
    session = tmp_path / "output" / "session_test_session"
    session.mkdir(parents=True)
    return str(session)


class TestResolvePathHappyPath:
    def test_plain_filename_lands_in_session_dir(self, session_dir):
        result = Path(resolve_path("report.md", session_dir))
        assert result == Path(session_dir) / "report.md"
        assert result.is_relative_to(Path(session_dir).resolve())

    def test_nested_relative_path_allowed(self, session_dir):
        result = Path(resolve_path("sub_dir/report.md", session_dir))
        assert result == Path(session_dir) / "sub_dir" / "report.md"

    def test_virtual_workspace_prefix_stripped(self, session_dir):
        result = Path(resolve_path("/workspace/report.md", session_dir))
        assert result == Path(session_dir) / "report.md"

    def test_virtual_mnt_data_prefix_stripped(self, session_dir):
        result = Path(resolve_path("/mnt/data/report.md", session_dir))
        assert result == Path(session_dir) / "report.md"

    def test_updated_prefix_collapsed_to_filename(self, session_dir):
        # 历史路径形式 updated/session_xxx/file.md 不再指向真实上传目录，
        # 而是折叠为会话目录内的同名文件
        result = Path(resolve_path("updated/session_abc/report.pdf", session_dir))
        assert result == Path(session_dir) / "report.pdf"

    def test_redundant_output_prefix_flattened(self, session_dir):
        # 模型把 output/session_xxx 前缀重复拼进相对路径时压平
        result = Path(resolve_path("output/session_test_session/report.md", session_dir))
        assert result == Path(session_dir) / "report.md"

    def test_normal_subdir_not_flattened(self, session_dir):
        result = Path(resolve_path("data/summary/report.md", session_dir))
        assert result == Path(session_dir) / "data" / "summary" / "report.md"


class TestResolvePathRejection:
    def test_parent_traversal_rejected(self, session_dir):
        with pytest.raises(PathEscapeError):
            resolve_path("../secrets.txt", session_dir)

    def test_deep_traversal_rejected(self, session_dir):
        with pytest.raises(PathEscapeError):
            resolve_path("sub/../../../../etc/passwd", session_dir)

    def test_windows_absolute_path_rejected(self, session_dir):
        with pytest.raises(PathEscapeError):
            resolve_path(r"C:\Users\someone\.env", session_dir)

    def test_unix_absolute_path_rejected(self, session_dir):
        with pytest.raises(PathEscapeError):
            resolve_path("/etc/passwd", session_dir)

    def test_updated_traversal_cannot_escape(self, session_dir):
        # updated 折叠为文件名后，无论输入多离谱都不允许逃出会话目录
        try:
            result = Path(resolve_path("updated/../../.env", session_dir))
        except PathEscapeError:
            return
        assert result.is_relative_to(Path(session_dir).resolve())

    def test_empty_filename_rejected(self, session_dir):
        with pytest.raises(PathEscapeError):
            resolve_path("", session_dir)

    def test_error_message_guides_model_to_retry(self, session_dir):
        with pytest.raises(PathEscapeError) as exc_info:
            resolve_path("../secrets.txt", session_dir)
        # 错误信息需要提示模型改用相对路径，便于 Agent 自我纠正重试
        assert "相对路径" in str(exc_info.value)
