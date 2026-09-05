"""
评测判分器（LLM-as-judge + 事实包含判定）

两类用例两套判分：
- SQL / 路由类：确定性判定，不调 LLM
  - SQL：expected_facts 全部作为子串出现在最终答案里即通过（大小写不敏感）
  - 路由：捕获到的工具/子智能体命中 expected_tools / expected_assistants 任一即通过
- Web / 多源类：调项目自带 LLM（app.agent.llm.model）做 LLM-as-judge，按维度打 1-5 分

LLM-as-judge 的已知偏差与缓解（写在这里供面试时能讲清）：
- 长度偏好（长答案易得高分）：提示词显式要求"不以长度论高低"；
- 位置偏好：要求按维度逐条给理由再给分；
- 仍是概率打分：评测脚本支持多次运行取中位数（--runs N），见 runner.py。
"""

import json
import re

from app.agent.llm import model


# ---------------------------------------------------------------------------
# 确定性判定（不调 LLM）
# ---------------------------------------------------------------------------

# 数字格式归一化：模型常把 160000 写成 "160,000" 或 "1,200,000.00"，
# 朴素子串匹配会因千分位/小数尾零误判 FAIL（sql-06/07/09 首跑基线实测暴露）。
# 归一化只作用于数字相邻字符，对中文药名等文本匹配无影响。
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d)")
_TRAILING_DOT_ZERO_RE = re.compile(r"(?<=\d)\.0+")


def _normalize_for_match(text: str) -> str:
    """匹配前归一化：小写化 + 去千分位逗号 + 去 .00 式小数尾零"""
    text = (text or "").lower()
    text = _THOUSANDS_RE.sub("", text)
    return _TRAILING_DOT_ZERO_RE.sub("", text)


def judge_sql(result: str, expected_facts: tuple[str, ...]) -> dict:
    """SQL 类：期望事实全部作为子串出现即通过（大小写不敏感、数字格式归一）"""
    text = _normalize_for_match(result)
    missing = [f for f in expected_facts
               if _normalize_for_match(f) not in text]
    return {
        "pass": len(missing) == 0,
        "score": 1.0 if not missing else 0.0,
        "detail": {"matched": [f for f in expected_facts if f not in missing],
                   "missing": missing},
    }


def judge_routing(tools: list[str], assistants: list[str],
                  expected_tools: tuple[str, ...],
                  expected_assistants: tuple[str, ...]) -> dict:
    """路由类：命中期望工具或子智能体任一即通过

    匹配语义：期望标识符是"记录名"的子串即算命中。monitor 上报的工具名
    带中文展示前缀（如 "数据库表数据查询工具：execute_sql_query"）、
    子智能体注册名是中文（如 "数据库查询助手"），因此期望值
    （"execute_sql_query" / "数据库查询助手"）必须是记录名的子串方向。
    旧实现方向写反（记录名 in 期望元组做精确成员判断），6 条路由用例
    全部误判 0 分——首跑基线实测暴露。
    """
    hit_tool = (
        any(exp in t for t in tools for exp in expected_tools)
        if expected_tools else False
    )
    hit_asst = (
        any(exp in a for a in assistants for exp in expected_assistants)
        if expected_assistants else False
    )
    passed = hit_tool or hit_asst
    return {
        "pass": passed,
        "score": 1.0 if passed else 0.0,
        "detail": {"tools_seen": tools, "assistants_seen": assistants,
                   "hit_tool": hit_tool, "hit_assistant": hit_asst},
    }


# ---------------------------------------------------------------------------
# LLM-as-judge（Web / 多源类）
# ---------------------------------------------------------------------------

_JUDGE_PROMPT = """你是一个严格的评测裁判。请对"智能体回答"在给定维度上打分。

【用户问题】
{query}

【智能体回答】
{answer}

【打分维度】每维 1-5 分（1=很差，3=及格，5=优秀），并给出一句理由：
{dims}

【评分规则】
- groundedness：回答是否有依据、是否可追溯到检索/数据来源，而非空泛编造；
- completeness：是否覆盖问题要点、信息完整；
- data_use：是否真正使用了数据库的事实（出现具体数字/药名/类别），而非只用网络空话；
- 不以长度论高低，简洁有据优于冗长空洞；
- 若回答明显编造或答非所问，各维度给 1 分。

只输出一行 JSON，格式：{{"维度": {{"score": N, "reason": "一句话"}}, ...}}，不要多余解释。"""


def judge_with_llm(query: str, answer: str, dims: tuple[str, ...]) -> dict:
    """调 LLM 按维度打分，返回 {dim: {score, reason}} 与平均分"""
    prompt = _JUDGE_PROMPT.format(
        query=query, answer=(answer or "(空)"), dims=" / ".join(dims)
    )
    try:
        resp = model.invoke(prompt)
        # LangChain 消息对象取 content
        content = resp.content if hasattr(resp, "content") else str(resp)
        scores = _parse_judge_json(content, dims)
    except Exception as e:
        return {"pass": False, "score": 0.0, "detail": {"error": f"judge 调用失败: {e}"}}

    avg = sum(s["score"] for s in scores.values()) / len(scores) if scores else 0.0
    # 5 分制归一到 0-1，>=3 分（及格）记通过
    return {
        "pass": avg >= 3.0,
        "score": round(avg / 5.0, 2),
        "detail": scores,
        "raw_avg": round(avg, 2),
    }


def _parse_judge_json(content: str, dims: tuple[str, ...]) -> dict:
    """从 LLM 输出里抽取 JSON 评分；解析失败时按 0 分兜底"""
    # 抓第一个 {...}
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return {d: {"score": 0, "reason": "解析失败"} for d in dims}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {d: {"score": 0, "reason": "JSON 解析失败"} for d in dims}
    result = {}
    for d in dims:
        v = obj.get(d, {})
        if isinstance(v, dict):
            score = int(v.get("score", 0))
            score = max(1, min(5, score))  # 钳到 1-5
            result[d] = {"score": score, "reason": v.get("reason", "")}
        else:
            result[d] = {"score": 0, "reason": "格式异常"}
    return result
