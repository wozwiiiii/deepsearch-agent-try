"""
评测 runner 限流退避测试：指数退避等待序列 + 限流后用例间短冷却

背景（v2 轮实测）：固定 70s×3 的退避在硅基流动 TPM 窗口未排空时会把
重试额度烧光（2 条用例重试耗尽报废，浪费评测额度）。改为指数退避
（60→120→240，总窗口 420s），且发生过限流的用例在下一条开始前追加 30s
短冷却，避免连环触发 429。

测试不发真实评测：run_deep_agent / select_cases / run_one / score_one /
asyncio.sleep 全部 monkeypatch 替身，只验证等待序列、重试次数与冷却触发
条件。429 错误沿生产路径注入（run_deep_agent 内部吞异常后经 monitor
上报 error 事件，runner 捕获的是 monitor 事件）。
"""

import asyncio
from argparse import Namespace

from app.api import monitor as monitor_mod
from eval import runner as runner_mod
from eval.cases import EvalCase


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def test_backoff_seconds_is_exponential():
    """重试等待按 2 的幂拉长：60 → 120 → 240（总窗口 420s）"""
    assert runner_mod.rate_limit_backoff_seconds(60.0, 0) == 60.0
    assert runner_mod.rate_limit_backoff_seconds(60.0, 1) == 120.0
    assert runner_mod.rate_limit_backoff_seconds(60.0, 2) == 240.0


def test_backoff_respects_custom_base():
    """基准秒数可调（--rate-limit-wait 透传）"""
    assert runner_mod.rate_limit_backoff_seconds(70.0, 1) == 140.0
    assert runner_mod.rate_limit_backoff_seconds(30.0, 2) == 120.0


def test_has_rate_limit_error_matches_429_only():
    """429 识别只看错误文本里的 '429'，其他错误不算限流"""
    assert runner_mod.has_rate_limit_error(
        ["执行主智能发生异常信息：Error code: 429 - TPM limit exceeded"]
    )
    assert runner_mod.has_rate_limit_error(["超时（180s）", "... 429 ..."])
    assert not runner_mod.has_rate_limit_error(["执行异常: 连接超时"])
    assert not runner_mod.has_rate_limit_error([])


# ---------------------------------------------------------------------------
# run_one：限流退避重试
# ---------------------------------------------------------------------------

_CASE = EvalCase(
    id="sql-04-count-drugs",
    query="数据库里一共有多少种药品？",
    category="sql",
    expected_facts=("50",),
)


def _patch_sleep(monkeypatch, waits):
    """把 runner 内的 asyncio.sleep 换成记录替身（避免真实等待）"""

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(runner_mod.asyncio, "sleep", fake_sleep)


def test_run_one_retries_with_exponential_backoff(monkeypatch):
    """429 持续出现：等待序列 60/120/240，共尝试 4 次，标记 rate_limited"""
    waits = []
    _patch_sleep(monkeypatch, waits)
    calls = []

    async def fake_run_deep_agent(query, thread_id, user_id="eval"):
        calls.append(thread_id)
        monitor_mod.monitor.report_error(
            "执行主智能发生异常信息：Error code: 429 - TPM limit exceeded"
        )
        return 123

    monkeypatch.setattr(runner_mod, "run_deep_agent", fake_run_deep_agent)

    run = asyncio.run(
        runner_mod.run_one(_CASE, timeout=5.0, rate_limit_wait=60.0, max_retries=3)
    )

    assert waits == [60.0, 120.0, 240.0]
    assert len(calls) == 4
    assert run["rate_limited"] is True
    assert run["tokens_used"] == 123


def test_run_one_recovers_after_one_backoff(monkeypatch):
    """第一次 429、第二次成功：只退避一次，成功结果被捕获"""
    waits = []
    _patch_sleep(monkeypatch, waits)
    attempts = []

    async def fake_run_deep_agent(query, thread_id, user_id="eval"):
        attempts.append(thread_id)
        if len(attempts) == 1:
            monitor_mod.monitor.report_error("执行异常: ... 429 ...")
            return 0
        monitor_mod.monitor.report_task_result("数据库里共有 50 种药品。")
        return 456

    monkeypatch.setattr(runner_mod, "run_deep_agent", fake_run_deep_agent)

    run = asyncio.run(
        runner_mod.run_one(_CASE, timeout=5.0, rate_limit_wait=60.0, max_retries=3)
    )

    assert waits == [60.0]
    assert len(attempts) == 2
    assert run["result"] == "数据库里共有 50 种药品。"
    assert run["rate_limited"] is True


def test_run_one_no_retry_without_rate_limit(monkeypatch):
    """非 429 错误不触发退避重试"""
    waits = []
    _patch_sleep(monkeypatch, waits)
    calls = []

    async def fake_run_deep_agent(query, thread_id, user_id="eval"):
        calls.append(thread_id)
        monitor_mod.monitor.report_error("执行主智能发生异常信息：连接超时")
        return 0

    monkeypatch.setattr(runner_mod, "run_deep_agent", fake_run_deep_agent)

    run = asyncio.run(
        runner_mod.run_one(_CASE, timeout=5.0, rate_limit_wait=60.0, max_retries=3)
    )

    assert waits == []
    assert len(calls) == 1
    assert run["rate_limited"] is False


# ---------------------------------------------------------------------------
# _run_all：限流用例后的短冷却
# ---------------------------------------------------------------------------

def _fake_args(pause=0.0, cooldown=30.0):
    return Namespace(
        case=None,
        category=None,
        runs=1,
        timeout=5.0,
        pause=pause,
        retries=3,
        rate_limit_wait=60.0,
        rate_limit_cooldown=cooldown,
        out=None,
    )


def _fake_run(case, rate_limited):
    return {
        "id": case.id,
        "category": case.category,
        "query": case.query,
        "result": "ok",
        "tools": [],
        "assistants": [],
        "errors": [],
        "tokens_used": 10,
        "rate_limited": rate_limited,
    }


def test_run_all_cooldowns_after_rate_limited_case(monkeypatch):
    """第 1 条用例限流 → 其后追加 30s 冷却；第 2 条无限流则不冷却"""
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(runner_mod.asyncio, "sleep", fake_sleep)

    cases = [
        _CASE,
        EvalCase(
            id="sql-05-count-cardio",
            query="数据库里有多少种心血管类的药品？",
            category="sql",
            expected_facts=("6",),
        ),
    ]

    async def fake_run_one(case, **kwargs):
        return _fake_run(case, rate_limited=(case.id == "sql-04-count-drugs"))

    monkeypatch.setattr(runner_mod, "select_cases", lambda args: cases)
    monkeypatch.setattr(runner_mod, "run_one", fake_run_one)
    monkeypatch.setattr(
        runner_mod,
        "score_one",
        lambda case, run: {"pass": True, "score": 1.0, "detail": {}},
    )

    exit_code = asyncio.run(runner_mod._run_all(_fake_args()))

    assert exit_code == 0
    # 只有第 1 条（限流）后冷却一次；pause=0 不追加用例间隔
    assert waits == [30.0]


def test_run_all_no_cooldown_when_no_rate_limit(monkeypatch):
    """无限流时无冷却等待（pause=0 时用例间零等待）"""
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(runner_mod.asyncio, "sleep", fake_sleep)

    cases = [_CASE]

    async def fake_run_one(case, **kwargs):
        return _fake_run(case, rate_limited=False)

    monkeypatch.setattr(runner_mod, "select_cases", lambda args: cases)
    monkeypatch.setattr(runner_mod, "run_one", fake_run_one)
    monkeypatch.setattr(
        runner_mod,
        "score_one",
        lambda case, run: {"pass": True, "score": 1.0, "detail": {}},
    )

    exit_code = asyncio.run(runner_mod._run_all(_fake_args()))

    assert exit_code == 0
    assert waits == []


def test_run_all_cooldown_skipped_after_last_case(monkeypatch):
    """最后一条用例限流也不追加冷却（后面没有用例了）"""
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(runner_mod.asyncio, "sleep", fake_sleep)

    async def fake_run_one(case, **kwargs):
        return _fake_run(case, rate_limited=True)

    monkeypatch.setattr(runner_mod, "select_cases", lambda args: [_CASE])
    monkeypatch.setattr(runner_mod, "run_one", fake_run_one)
    monkeypatch.setattr(
        runner_mod,
        "score_one",
        lambda case, run: {"pass": True, "score": 1.0, "detail": {}},
    )

    exit_code = asyncio.run(runner_mod._run_all(_fake_args()))

    assert exit_code == 0
    assert waits == []
