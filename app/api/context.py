"""
请求上下文管理模块

负责在异步请求链路中保存当前任务的 thread_id 和 session_dir
工具、智能体和监控模块可以在深层调用中读取这些值，而不需要层层传参
"""

import uuid
from contextvars import ContextVar, Token
from typing import Optional

# ContextVar 是协程级上下文变量，适合 FastAPI 这类异步 Web 服务
# 它可以避免多个并发请求共用全局变量时出现 thread_id 或 session_dir 串台
_session_dir_ctx: ContextVar[Optional[str]] = ContextVar(
    "session_dir",
    default=None,
)
_thread_id_ctx: ContextVar[Optional[str]] = ContextVar(
    "thread_id",
    default=None,
)
_trace_id_ctx: ContextVar[Optional[str]] = ContextVar(
    "trace_id",
    default=None,
)
_user_id_ctx: ContextVar[Optional[str]] = ContextVar(
    "user_id",
    default=None,
)


def set_session_context(path: str) -> Token[Optional[str]]:
    """
    设置当前请求链路的会话目录

    :param path: 当前任务的工作目录
    :return: reset 时需要使用的上下文 token
    """
    return _session_dir_ctx.set(path)


def get_session_context() -> Optional[str]:
    """
    获取当前请求链路的会话目录

    :return: 当前任务工作目录；未设置时返回 None
    """
    return _session_dir_ctx.get()


def set_thread_context(thread_id: str) -> Token[Optional[str]]:
    """
    设置当前请求链路的线程 ID

    :param thread_id: 前端连接和 Agent 执行共用的任务 ID
    :return: reset 时需要使用的上下文 token
    """
    return _thread_id_ctx.set(thread_id)


def get_thread_context() -> Optional[str]:
    """
    获取当前请求链路的线程 ID

    :return: 当前任务 ID；未设置时返回 None
    """
    return _thread_id_ctx.get()


def set_trace_context() -> tuple[str, Token[Optional[str]]]:
    """
    生成并设置当前请求链路的 trace_id

    用于故障定位：同一请求在 API 层 → agent 层 → tools 层打出的日志
    共享同一个 trace_id，可按其秒级聚合定位。

    :return: (trace_id, reset 时需要的上下文 token)
    """
    trace_id = uuid.uuid4().hex[:16]
    return trace_id, _trace_id_ctx.set(trace_id)


def get_trace_context() -> Optional[str]:
    """
    获取当前请求链路的 trace_id

    :return: 当前 trace_id；未设置时返回 None
    """
    return _trace_id_ctx.get()


def set_user_context(user_id: str) -> Token[Optional[str]]:
    """
    设置当前请求链路的租户 user_id

    :param user_id: 租户身份
    :return: reset 时需要的上下文 token
    """
    return _user_id_ctx.set(user_id)


def get_user_context() -> Optional[str]:
    """
    获取当前请求链路的租户 user_id

    :return: 当前 user_id；未设置时返回 None
    """
    return _user_id_ctx.get()


def reset_session_context(
    session_token: Token[Optional[str]],
    thread_token: Optional[Token[Optional[str]]] = None,
    trace_token: Optional[Token[Optional[str]]] = None,
    user_token: Optional[Token[Optional[str]]] = None,
) -> None:
    """
    恢复请求上下文，避免本次任务信息残留到后续请求

    :param session_token: set_session_context 返回的 token
    :param thread_token: set_thread_context 返回的 token
    :param trace_token: set_trace_context 返回的 token
    :param user_token: set_user_context 返回的 token
    """
    _session_dir_ctx.reset(session_token)
    if thread_token is not None:
        _thread_id_ctx.reset(thread_token)
    if trace_token is not None:
        _trace_id_ctx.reset(trace_token)
    if user_token is not None:
        _user_id_ctx.reset(user_token)
