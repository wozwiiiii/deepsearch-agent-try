"""
文件路径解析工具

负责把模型或工具返回的虚拟路径、上传文件路径和相对路径统一转换为本地绝对路径。
安全契约（生产化改造后）：

1. 所有解析结果必须落在当前 session_dir 内，越界路径一律抛出 PathEscapeError；
2. 绝对路径（含 Windows 盘符路径和 / 开头的 Unix 路径）不再放行，模型必须使用
   会话目录内的相对路径，报错信息会引导模型自我纠正；
3. 不再存在绕过会话约束的 `updated/` 特例分支，历史路径统一折叠为文件名。
"""

import os
from pathlib import Path
from typing import Optional


class PathEscapeError(ValueError):
    """路径尝试逃出会话目录或使用被禁止的路径形式时抛出"""

    def __init__(self, filename: str, reason: str):
        self.filename = filename
        super().__init__(
            f"路径被拒绝: {filename}（{reason}）。"
            f"请只使用当前工作目录内的相对路径，例如 'report.md'。"
        )


# 大模型常返回 /workspace、/mnt/data 这类虚拟沙箱前缀，解析前统一剥离
ALLOWED_VIRTUAL_PREFIXES = ("/workspace", "/mnt/data", "/home/user")


def resolve_path(filename: str, session_dir: Optional[str] = None) -> str:
    """
    解析文件路径，并把结果强制限制在当前会话目录中

    :param filename: 模型、工具或用户传入的文件名/路径
    :param session_dir: 当前任务的会话目录；提供时强制收容校验
    :return: 解析后的绝对路径，保证位于 session_dir 内
    :raises PathEscapeError: 路径为空、使用绝对路径或尝试逃出会话目录
    """
    raw = filename if isinstance(filename, str) else str(filename)
    path_str = raw.replace("\\", "/").strip()

    if not path_str or path_str == "/":
        raise PathEscapeError(raw, "文件名不能为空")

    # 剥离大模型常见的虚拟沙箱前缀，例如 /workspace/report.md -> report.md
    for prefix in ALLOWED_VIRTUAL_PREFIXES:
        if path_str.startswith(prefix + "/"):
            path_str = path_str[len(prefix) + 1 :]
            break

    # 历史版本允许 updated/ 前缀指向真实上传目录，这会绕过会话约束；
    # 现在上传文件在任务启动时已复制进 session_dir，这里统一折叠为文件名
    if "updated/" in path_str:
        path_str = path_str.split("updated/")[-1].split("/")[-1]

    if not path_str or path_str == "/":
        raise PathEscapeError(raw, "剥离前缀后文件名为空")

    if session_dir is None:
        # 无会话上下文的本地脚本调试场景：只允许相对路径
        if os.name == "nt" and Path(path_str).is_absolute():
            raise PathEscapeError(raw, "无会话上下文时禁止使用绝对路径")
        return str(Path(path_str).resolve())

    session_path = Path(session_dir).resolve()

    if Path(path_str).is_absolute():
        # 绝对路径一律拒绝：即使指向会话目录内部也要求改用相对形式，
        # 避免 Windows/Unix 路径差异导致的校验绕过
        raise PathEscapeError(raw, "禁止使用绝对路径")

    resolved = (session_path / path_str).resolve()

    if not resolved.is_relative_to(session_path):
        # resolve 会展开 ../ 等间接形式，这里统一做收容校验
        raise PathEscapeError(raw, "路径越出会话目录")

    return str(_flatten_redundant_prefix(resolved, session_path))


def _flatten_redundant_prefix(full_path: Path, session_path: Path) -> Path:
    """
    压平模型误拼的冗余目录层级

    模型偶尔会生成 output/session_xxx/xxx.md 或 session_xxx/session_xxx/xxx.md
    这类把会话目录名或 output 前缀重复拼进相对路径的写法；这里把中间的
    冗余层级去掉，只保留最深层的子目录和文件名结构。
    """
    rel_parts = full_path.relative_to(session_path).parts
    redundant = {session_path.name, "output", "updated"}
    if len(rel_parts) > 1 and any(part in redundant for part in rel_parts[:-1]):
        return session_path / rel_parts[-1]
    return full_path
