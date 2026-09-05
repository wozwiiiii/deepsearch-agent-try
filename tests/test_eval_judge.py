"""
评测判分器测试：judge_routing 的记录名/期望名匹配语义 + judge_sql 数字归一化

背景（首跑基线实测暴露的 bug）：
- routing：monitor 上报的工具名带中文展示前缀（"数据库表数据查询工具：
  execute_sql_query"）、子智能体注册名为中文（"数据库查询助手"）；旧实现
  把匹配方向写反（记录名 in 期望元组做精确成员判断），导致路由类用例
  无论实际路由是否正确一律 0 分。
- sql：模型常把 160000 写成 "160,000" 或 "1,200,000.00"，朴素子串匹配
  因千分位/小数尾零误判 FAIL（sql-06/07/09 首跑基线实测暴露）。
"""

from eval.judge import _normalize_for_match, judge_routing, judge_sql


def test_expected_function_name_hits_prefixed_recorded_name():
    """期望函数名是记录名子串即命中（工具名带中文前缀）"""
    verdict = judge_routing(
        tools=["数据库表名查询工具：list_sql_tables",
               "数据库表数据查询工具：execute_sql_query"],
        assistants=["数据库查询助手"],
        expected_tools=("execute_sql_query",),
        expected_assistants=("数据库查询助手",),
    )
    assert verdict["pass"] is True
    assert verdict["score"] == 1.0
    assert verdict["detail"]["hit_tool"] is True
    assert verdict["detail"]["hit_assistant"] is True


def test_expected_assistant_hits_registered_chinese_name():
    """子智能体注册名为中文，期望名精确一致即命中"""
    verdict = judge_routing(
        tools=["网络搜索工具"],
        assistants=["网络搜索助手"],
        expected_tools=(),
        expected_assistants=("网络搜索助手",),
    )
    assert verdict["pass"] is True
    assert verdict["detail"]["hit_assistant"] is True
    assert verdict["detail"]["hit_tool"] is False


def test_no_hit_scores_zero():
    """路由到错误助手时不得分"""
    verdict = judge_routing(
        tools=["网络搜索工具"],
        assistants=["网络搜索助手"],
        expected_tools=("execute_sql_query",),
        expected_assistants=("数据库查询助手",),
    )
    assert verdict["pass"] is False
    assert verdict["score"] == 0.0


def test_empty_captured_means_no_hit():
    """实际没调用任何工具/助手时不得分（429 中断等场景）"""
    verdict = judge_routing(
        tools=[],
        assistants=[],
        expected_tools=("execute_sql_query",),
        expected_assistants=("数据库查询助手",),
    )
    assert verdict["pass"] is False


def test_expected_when_nothing_expected_is_no_hit():
    """期望为空时不应因记录非空而误判通过"""
    verdict = judge_routing(
        tools=["任意工具"],
        assistants=["任意助手"],
        expected_tools=(),
        expected_assistants=(),
    )
    assert verdict["pass"] is False


# ---------------------------------------------------------------------------
# judge_sql：数字格式归一化
# ---------------------------------------------------------------------------

def test_sql_thousands_separator_still_matches():
    """模型写 '160,000' 应命中期望 '160000'（sql-06 首跑误判场景）"""
    verdict = judge_sql(
        "根据数据库查询的结果，连花清瘟胶囊的总库存量为160,000盒。",
        expected_facts=("160000",),
    )
    assert verdict["pass"] is True
    assert verdict["detail"]["missing"] == []


def test_sql_trailing_dot_zero_still_matches():
    """模型写 '1,200,000.00' 应命中期望 '1200000'（sql-09 首跑误判场景）"""
    verdict = judge_sql(
        "总销售额为 1,200,000.00 元。",
        expected_facts=("1200000",),
    )
    assert verdict["pass"] is True


def test_sql_real_mismatch_still_fails():
    """数值真不同时仍判 FAIL（sql-18 实测答 94000 vs 真值 47000）"""
    verdict = judge_sql(
        "总库存量为 94,000 个单位，总销售额为 270,000 元。",
        expected_facts=("47000", "90000"),
    )
    assert verdict["pass"] is False
    assert set(verdict["detail"]["missing"]) == {"47000", "90000"}


def test_sql_normalization_preserves_chinese_matching():
    """归一化不影响中文药名匹配"""
    assert _normalize_for_match("阿莫西林胶囊") == "阿莫西林胶囊"
    verdict = judge_sql("立普妥的通用名是阿托伐他汀钙片。", ("阿托伐他汀钙片",))
    assert verdict["pass"] is True
