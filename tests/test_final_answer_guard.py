"""
最终回答守卫测试：拦截"纯过渡语早停"（P1 评测修复）

背景（评测 90 次运行实测 5 次同签名早停）：主智能体偶发以"我将启动数据库
查询助手进行查询。""我将等待其结果。"这类纯过渡语收尾，用户只收到一句
空话。守卫在回合自然结束后识别该特征并强制续跑一次；续跑后仍过渡语则
放行结束（防死循环）。

测试分两层：
- is_pure_transition_reply 纯函数判据（高精度：短文本 + 无数字 + 过渡语短语）；
- _consume_agent_stream 守卫循环（假 agent 按脚本回放 astream 片段，
  monitor 用记录替身，不发真实请求，不产生费用）。
"""

import asyncio

from app.agent import final_answer_guard as guard_mod
from app.agent import main_agent as main_agent_module
from app.agent.final_answer_guard import (
    FINAL_ANSWER_GUARD_PROMPT,
    is_pure_transition_reply,
)


# ---------------------------------------------------------------------------
# 判据纯函数
# ---------------------------------------------------------------------------

def test_real_early_stop_samples_are_detected():
    """评测实测的两条早停样本必须命中"""
    assert is_pure_transition_reply("我将启动数据库查询助手进行查询。") is True
    assert is_pure_transition_reply("我将等待其结果。") is True


def test_more_transition_phrases_detected():
    """其余过渡语短语同样命中"""
    for text in ("让我来查一下。", "正在查询中，请稍候。", "即将为您检索相关资料。"):
        assert is_pure_transition_reply(text) is True, text


def test_short_substantive_answer_not_detected():
    """一句话实质答案绝不能触发守卫"""
    assert is_pure_transition_reply("阿莫西林是青霉素类抗生素。") is False  # 无过渡语
    assert is_pure_transition_reply("未查询到相关药品信息。") is False  # 无过渡语


def test_numbers_anywhere_disqualify():
    """过渡语里混入任何数字即视为携带数据，放行（'共有 50 种药品'场景）"""
    assert is_pure_transition_reply("数据库里共有 50 种药品。") is False
    assert is_pure_transition_reply("我将为您查询 3 项结果。") is False
    # 全角数字同样放行
    assert is_pure_transition_reply("我将为您查询３项结果。") is False


def test_long_text_not_detected():
    """超过长度上限的文本不做拦截（完整报告/长回答）"""
    long_text = "我将为您整理如下：" + "详细说明内容。" * 40
    assert len(long_text) >= guard_mod.FINAL_ANSWER_MAX_CHARS
    assert is_pure_transition_reply(long_text) is False


def test_non_string_and_empty_not_detected():
    """非字符串（内容块列表等）与空文本一律保守放行"""
    assert is_pure_transition_reply(None) is False
    assert is_pure_transition_reply("") is False
    assert is_pure_transition_reply("   ") is False
    assert is_pure_transition_reply([{"type": "text", "text": "我将等待其结果。"}]) is False


# ---------------------------------------------------------------------------
# _consume_agent_stream 守卫循环
# ---------------------------------------------------------------------------

class _Msg:
    """最小化的模型消息替身（AIMessage 的鸭子类型子集）"""

    def __init__(self, content="", tool_calls=None, total_tokens=100):
        self.content = content
        self.tool_calls = tool_calls or []
        self.usage_metadata = {
            "input_tokens": total_tokens - 10,
            "output_tokens": 10,
            "total_tokens": total_tokens,
        }


class _RecordingMonitor:
    """记录 task_result / assistant 事件的替身 monitor"""

    def __init__(self):
        self.results = []
        self.assistants = []

    def report_task_result(self, result):
        self.results.append(result)

    def report_assistant(self, name, args=None):
        self.assistants.append(name)


class _ScriptedAgent:
    """按脚本逐轮回放 astream 片段，并记录每次收到的输入与 config"""

    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.inputs = []
        self.configs = []

    async def astream(self, agent_input, config=None):
        self.inputs.append(agent_input)
        self.configs.append(config)
        for chunk in self._rounds.pop(0):
            yield chunk


def _model_chunk(content="", tool_calls=None, total_tokens=100):
    return {"model": {"messages": [_Msg(content, tool_calls, total_tokens)]}}


def _task_tool_call():
    """DeepAgents 的子智能体调用本质是名为 task 的工具调用"""
    return [
        {
            "name": "task",
            "args": {
                "subagent_type": "数据库查询助手",
                "description": "查询库存",
            },
        }
    ]


def _assert_same_thread(agent):
    """续跑轮必须复用首轮的 thread_id（守卫在同一会话内注入纠偏消息续跑）"""
    first = agent.configs[0]["configurable"]["thread_id"]
    second = agent.configs[1]["configurable"]["thread_id"]
    assert second == first == "t-guard"


def _run_stream(monkeypatch, rounds):
    """运行 _consume_agent_stream，返回 (假 agent, 替身 monitor, tokens)"""
    agent = _ScriptedAgent(rounds)
    stub_monitor = _RecordingMonitor()

    async def _factory():
        return agent

    monkeypatch.setattr(main_agent_module, "monitor", stub_monitor)
    tokens = asyncio.run(
        main_agent_module._consume_agent_stream(
            _factory, "查询库存", {"configurable": {"thread_id": "t-guard"}}
        )
    )
    return agent, stub_monitor, tokens


def test_transition_stop_triggers_guard_then_real_answer(monkeypatch):
    """纯过渡语收尾 → 守卫注入纠偏消息续跑 → 得到实质回答"""
    agent, stub_monitor, tokens = _run_stream(
        monkeypatch,
        [
            [_model_chunk(content="我将启动数据库查询助手进行查询。")],
            [
                _model_chunk(tool_calls=_task_tool_call()),
                _model_chunk(content="数据库里共有 50 种药品。"),
            ],
        ],
    )
    # 恰好续跑一轮，第二轮输入是纠偏消息
    assert len(agent.inputs) == 2
    assert agent.inputs[1]["messages"][0]["content"] == FINAL_ANSWER_GUARD_PROMPT
    _assert_same_thread(agent)
    # 过渡语和实质回答都会上报，前端/评测取最后一条
    assert stub_monitor.results == [
        "我将启动数据库查询助手进行查询。",
        "数据库里共有 50 种药品。",
    ]
    assert tokens == 300


def test_subagent_dispatched_but_wait_reply_recovered(monkeypatch):
    """sql-08 变体：已派发子智能体仍以'我将等待其结果'收尾，同样触发守卫"""
    agent, stub_monitor, _ = _run_stream(
        monkeypatch,
        [
            [
                _model_chunk(tool_calls=_task_tool_call()),
                _model_chunk(content="我将等待其结果。"),
            ],
            [_model_chunk(content="库存最多的药品是阿莫西林，共 500 盒。")],
        ],
    )
    assert len(agent.inputs) == 2
    assert agent.inputs[1]["messages"][0]["content"] == FINAL_ANSWER_GUARD_PROMPT
    _assert_same_thread(agent)
    assert stub_monitor.results[-1] == "库存最多的药品是阿莫西林，共 500 盒。"


def test_normal_answer_no_guard(monkeypatch):
    """正常简短实质回答不触发守卫（只跑一轮）"""
    agent, stub_monitor, _ = _run_stream(
        monkeypatch,
        [[_model_chunk(content="数据库里共有 50 种药品。")]],
    )
    assert len(agent.inputs) == 1
    assert stub_monitor.results == ["数据库里共有 50 种药品。"]


def test_guard_fires_at_most_once(monkeypatch):
    """续跑后仍是过渡语 → 放行结束，不再第三轮（防死循环）"""
    agent, stub_monitor, _ = _run_stream(
        monkeypatch,
        [
            [_model_chunk(content="我将启动数据库查询助手进行查询。")],
            [_model_chunk(content="让我稍等片刻再查看结果。")],
        ],
    )
    assert len(agent.inputs) == 2
    # 续跑轮仍复用首轮 thread_id（同一会话续跑，只是放行结束）
    _assert_same_thread(agent)
    # 第二轮过渡语作为最终结果放行上报
    assert stub_monitor.results[-1] == "让我稍等片刻再查看结果。"


def test_tool_call_only_end_does_not_trigger_guard(monkeypatch):
    """回合以工具调用片段结束（无最终文本）时守卫不介入"""
    agent, _, _ = _run_stream(
        monkeypatch,
        [[_model_chunk(tool_calls=_task_tool_call())]],
    )
    assert len(agent.inputs) == 1
