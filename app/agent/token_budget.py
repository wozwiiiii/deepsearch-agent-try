"""会话级 token 预算（P1：run 级熔断的跨任务扩展）

背景：run 级熔断（MODEL_TOKEN_RUN_LIMIT=150 万/任务，见 main_agent）只能
拦截单任务爆炸，拦不住同一会话（thread_id）跨多次任务的累计消耗——长会话
高频使用时总成本失控。

实现：进程内 {thread_id: 累计 tokens} 字典，main_agent 每次 astream 轮
结束把本轮消耗记入并检查会话上限，超限抛 RuntimeError（与 run 级熔断
同风格，由 run_deep_agent 统一捕获上报）。

如实说明边界：
- 字典随进程存活：进程重启后清零。会话历史本身在 checkpointer（SQLite）
  里，但已消耗的 token 属于沉没成本，重新计数不影响熔断语义；
- 单进程部署假设成立（当前架构无多 worker）。P0-2 任务出进程落地后，
  此模块需迁移到共享存储（如 Redis），否则各进程独立计数、会话级上限
  实际放宽为 N×上限（N=进程数）；
- 阈值默认 3×run 级（450 万）：允许一个会话内完成多轮常规任务，同时
  封死无限累积。经 MODEL_TOKEN_SESSION_LIMIT 环境变量调整。
"""

import os
import threading

# 会话级 token 预算：同一 thread_id 跨任务累计上限（默认 3×run 级）
MODEL_TOKEN_SESSION_LIMIT = int(os.getenv("MODEL_TOKEN_SESSION_LIMIT", "4500000"))

# {thread_id: 累计 tokens}。asyncio 单线程事件循环内读改写无竞态；
# 加锁只为防御未来在多线程中复用此模块（成本可忽略）
_lock = threading.Lock()
_session_usage: dict[str, int] = {}


def record_session_tokens(thread_id: str, tokens: int) -> int:
    """把本轮消耗记入会话累计，返回累计值

    :param thread_id: 会话 ID（复合熔断维度）
    :param tokens: 本轮消耗（usage 缺失时传 0，不影响累计）
    :return: 该会话的累计 token 消耗
    """
    with _lock:
        current = _session_usage.get(thread_id, 0) + max(0, tokens)
        _session_usage[thread_id] = current
        return current


def session_budget_exceeded(total: int) -> bool:
    """会话累计是否已超预算"""
    return total > MODEL_TOKEN_SESSION_LIMIT


def reset_session_usage(thread_id: str | None = None) -> None:
    """清零会话累计（测试用；thread_id 为 None 时全量清零）"""
    with _lock:
        if thread_id is None:
            _session_usage.clear()
        else:
            _session_usage.pop(thread_id, None)
