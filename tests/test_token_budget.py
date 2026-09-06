"""会话级 token 预算（token_budget）单元测试

覆盖：累计记录、阈值边界、超限判定、重置语义、main_agent 循环内的
超限熔断路径（mock astream 脚本化注入大 usage）。不发真实请求。
"""

import asyncio

from app.agent import main_agent as main_agent_module
from app.agent import token_budget as budget_mod
from app.agent.token_budget import (
    MODEL_TOKEN_SESSION_LIMIT,
    record_session_tokens,
    reset_session_usage,
    session_budget_exceeded,
)


def setup_function(_):
    """每个测试独立的累计桶，避免用例间串扰"""
    reset_session_usage()


def test_record_accumulates_per_thread():
    assert record_session_tokens("t-a", 100) == 100
    assert record_session_tokens("t-a", 200) == 300
    # 不同 thread_id 独立累计
    assert record_session_tokens("t-b", 50) == 50
    assert record_session_tokens("t-a", 1) == 301


def test_record_ignores_negative_tokens():
    """usage 异常负值不计入（防御性）"""
    assert record_session_tokens("t-neg", -5) == 0


def test_budget_exceeded_boundary():
    """超限判定：等于上限不算超，超过才算（与 run 级 > 语义一致）"""
    assert not session_budget_exceeded(MODEL_TOKEN_SESSION_LIMIT)
    assert session_budget_exceeded(MODEL_TOKEN_SESSION_LIMIT + 1)


def test_reset_session_usage():
    record_session_tokens("t-r", 999)
    reset_session_usage("t-r")
    assert record_session_tokens("t-r", 1) == 1
    record_session_tokens("t-r2", 500)
    reset_session_usage()
    assert record_session_tokens("t-r2", 1) == 1


def test_main_agent_session_budget_fuse(monkeypatch):
    """main_agent 循环内：会话累计超上限 → RuntimeError 熔断

    脚本：单轮 astream 注入超过会话上限的 usage（绕过 run 级检查的
    前提是 run 级未超——把 run 级环境值调大，只让会话级触发）。
    """
    reset_session_usage()
    # 两处常量都要打补丁：比较逻辑读 token_budget 模块内全局，
    # 错误消息读 main_agent 模块内全局
    monkeypatch.setattr(budget_mod, "MODEL_TOKEN_SESSION_LIMIT", 5000)
    monkeypatch.setattr(
        main_agent_module, "MODEL_TOKEN_SESSION_LIMIT", 5000, raising=False
    )

    class _Msg:
        content = "正常回答"

        def __init__(self, total):
            self.tool_calls = []
            self.usage_metadata = {"total_tokens": total}

    class _Agent:
        async def astream(self, agent_input, config=None):
            yield {"model": {"messages": [_Msg(6000)]}}

    async def _factory():
        return _Agent()

    config = {"configurable": {"thread_id": "t-fuse"}}
    try:
        asyncio.run(main_agent_module._consume_agent_stream(_factory, "问题", config))
        raise AssertionError("应当触发会话级熔断 RuntimeError")
    except RuntimeError as e:
        assert "会话" in str(e) and "预算上限" in str(e)
    # 熔断前的消耗已计入会话累计
    assert record_session_tokens("t-fuse", 0) == 6000


def test_main_agent_session_budget_not_triggered_below_limit(monkeypatch):
    """会话累计未超限 → 正常完成，不做熔断"""
    reset_session_usage()
    monkeypatch.setattr(budget_mod, "MODEL_TOKEN_SESSION_LIMIT", 5000)
    monkeypatch.setattr(
        main_agent_module, "MODEL_TOKEN_SESSION_LIMIT", 5000, raising=False
    )

    class _RecordingMonitor:
        def __init__(self):
            self.results = []

        def report_task_result(self, r):
            self.results.append(r)

        def report_assistant(self, *a, **k):
            pass

    class _Msg:
        def __init__(self, content, total):
            self.content = content
            self.tool_calls = []
            self.usage_metadata = {"total_tokens": total}

    class _Agent:
        async def astream(self, agent_input, config=None):
            yield {"model": {"messages": [_Msg("答案内容", 4000)]}}

    async def _factory():
        return _Agent()

    stub = _RecordingMonitor()
    monkeypatch.setattr(main_agent_module, "monitor", stub)
    config = {"configurable": {"thread_id": "t-ok"}}
    tokens = asyncio.run(
        main_agent_module._consume_agent_stream(_factory, "问题", config)
    )
    assert stub.results == ["答案内容"]
    assert tokens == 4000
