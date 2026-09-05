"""评测报告离线后处理：污染标记 + 判分修正（不重跑 Agent，零 API 成本）

背景（首轮基线 2026-09-05 实测暴露）：
1. 部分用例因外部资源故障未获公平评测（硅基流动 429 限流、超时、
   Tavily 搜索代理连接失败 ProxyError），
   需打 `contaminated` 标记，统计与补测决策时单独看待；
2. judge 自身缺陷导致误判：sql 类数字千分位/小数尾零（"160,000" vs
   "160000"）、multi 类 judge LLM 输出 JSON 解析失败兜底 0 分。
   judge 修复后基于已捕获的 result/tools/assistants 重算，无需重跑。

用法：
    python -m eval.rejudge eval-report-sql.json              # 标记 + sql 重判
    python -m eval.rejudge eval-report-multi.json            # 标记 + multi 解析失败重判（调 judge LLM，开销极小）
    python -m eval.rejudge eval-report-web.json --no-llm     # 仅标记，不调 LLM

行为约定：
- 首次写回前生成 `<report>.bak` 备份（报告未入 git，防误覆盖）；
- 幂等：重复执行不重复打标、不重复追加 note；
- 判分变更保留原始 verdict 到 `verdict_prev`，并写 `rejudge_reason`，
  保证原始判定可追溯（实事求是：修正过程可审计）。
"""

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from eval.cases import CASES
from eval.judge import judge_sql, judge_with_llm

_CASE_BY_ID = {c.id: c for c in CASES}


# ---------------------------------------------------------------------------
# 污染标记
# ---------------------------------------------------------------------------

def _contamination_reason(errors: list[str]) -> str | None:
    """根据错误信息判定污染类型；无外部故障返回 None"""
    text = " | ".join(errors or [])
    if "429" in text:
        return "rate_limit_429"
    if "超时" in text or "timed out" in text:
        return "timeout"
    if "ProxyError" in text or "tavily.com" in text:
        # Tavily 搜索经代理连接失败：外部搜索服务故障，非智能体缺陷
        return "external_search_down"
    return None


def mark_contamination(case: dict) -> bool:
    """打 contaminated 标记，返回是否有变更"""
    reason = _contamination_reason(case.get("errors", []))
    if reason is None:
        return False
    changed = False
    if not case.get("contaminated"):
        case["contaminated"] = True
        changed = True
    if case.get("contamination_reason") != reason:
        case["contamination_reason"] = reason
        changed = True
    return changed


def mark_manual(case: dict, reason: str, evidence: str) -> bool:
    """人工标记通道：报告内 errors 无法自动判定、但运行日志有明确证据时使用。

    必须提供 evidence（标注依据），保证可审计。返回是否有变更。
    """
    changed = False
    if not case.get("contaminated"):
        case["contaminated"] = True
        changed = True
    if case.get("contamination_reason") != reason:
        case["contamination_reason"] = reason
        changed = True
    if case.get("contamination_evidence") != evidence:
        case["contamination_evidence"] = evidence
        changed = True
    return changed


# ---------------------------------------------------------------------------
# 判分修正
# ---------------------------------------------------------------------------

def rejudge_sql(case: dict) -> bool:
    """用修复后的 judge_sql 重算 sql 类用例，返回是否有变更"""
    case_def = _CASE_BY_ID.get(case["id"])
    if case_def is None:
        return False
    new_verdict = judge_sql(case.get("result") or "", case_def.expected_facts)
    if new_verdict == case.get("verdict"):
        return False
    case["verdict_prev"] = case.get("verdict")
    case["verdict"] = new_verdict
    case["rejudge_reason"] = "judge bug fix: 数字千分位/小数尾零归一化（离线重判，未重跑 Agent）"
    return True


def _judge_infra_failure(case: dict) -> bool:
    """judge 基础设施失败（非智能体失败）：judge LLM 输出解析失败兜底 0 分"""
    detail = (case.get("verdict") or {}).get("detail") or {}
    scores = detail.get("runs") or [detail]
    for d in scores:
        for dim in (d.get("groundedness"), d.get("completeness"), d.get("data_use")):
            if isinstance(dim, dict) and dim.get("reason") in ("JSON 解析失败", "格式异常", "解析失败"):
                return True
        if isinstance(d, dict) and "error" in d and "judge 调用失败" in str(d["error"]):
            return True
    return False


def rejudge_multi(case: dict) -> bool:
    """multi 类用例 judge 解析失败时重调一次 judge LLM，返回是否有变更"""
    if not _judge_infra_failure(case):
        return False
    case_def = _CASE_BY_ID.get(case["id"])
    if case_def is None:
        return False
    new_verdict = judge_with_llm(
        case_def.query, case.get("result") or "", case_def.judge_dims
    )
    if new_verdict.get("pass") is False and "judge 调用失败" in str(new_verdict.get("detail", {})):
        # judge 自身再失败：保留原判，不强改（避免以失败覆盖失败）
        print(f"    {case['id']}: judge 重试仍失败，保留原判", flush=True)
        return False
    case["verdict_prev"] = case.get("verdict")
    case["verdict"] = new_verdict
    case["rejudge_reason"] = "judge LLM 输出解析失败重试（离线重判，未重跑 Agent）"
    return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

_NOTE_TAG = "rejudge_note_appended"

_NOTE_SQL = (
    "judge bug 修复后离线重判：sql 类数字千分位/小数尾零归一化；"
    "污染用例标记 contaminated（429 限流/超时未获公平评测）。"
    "原始判定保留在各用例 verdict_prev，可审计。"
)
_NOTE_MULTI = (
    "judge LLM 解析失败用例重判 + 污染用例标记（离线，未重跑 Agent）。"
    "原始判定保留在各用例 verdict_prev，可审计。"
)
_NOTE_MARK_ONLY = "污染用例标记 contaminated（429 限流/超时/Tavily 搜索故障未获公平评测）。"


def process(path: str, allow_llm: bool, manual_marks: dict[str, tuple[str, str]] | None = None) -> int:
    report_path = Path(path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    cases = report.get("cases", [])
    category = cases[0].get("category") if cases else None
    manual_marks = manual_marks or {}

    n_marked = n_rejudged = 0
    for case in cases:
        if mark_contamination(case):
            n_marked += 1
            print(f"  [mark] {case['id']}: contaminated ({case.get('contamination_reason')})")
        if case["id"] in manual_marks:
            reason, evidence = manual_marks[case["id"]]
            if mark_manual(case, reason, evidence):
                n_marked += 1
                print(f"  [mark-manual] {case['id']}: contaminated ({reason}) 依据: {evidence}")
        if category == "sql" and rejudge_sql(case):
            n_rejudged += 1
            print(f"  [rejudge] {case['id']}: {case['verdict_prev'].get('pass')} -> {case['verdict'].get('pass')}")
        if category == "multi" and allow_llm and rejudge_multi(case):
            n_rejudged += 1
            print(f"  [rejudge] {case['id']}: {case['verdict_prev'].get('score')} -> {case['verdict'].get('score')}")

    # 重算汇总
    passed = sum(1 for c in cases if c.get("verdict", {}).get("pass"))
    report["passed"] = passed
    report["pass_rate"] = round(passed / len(cases), 3) if cases else 0.0
    report["rejudged_at"] = datetime.now().isoformat()

    note = {"sql": _NOTE_SQL, "multi": _NOTE_MULTI}.get(
        category, _NOTE_MARK_ONLY
    ) if n_rejudged else _NOTE_MARK_ONLY
    if not report.get("note"):
        report["note"] = note

    if n_marked or n_rejudged:
        bak = report_path.with_suffix(".json.bak")
        if not bak.exists():
            shutil.copy2(report_path, bak)
            print(f"  原始报告已备份：{bak.name}")
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(
        f"{report_path.name}: 标记 {n_marked} 条，重判 {n_rejudged} 条 -> "
        f"通过 {passed}/{len(cases)}（{report['pass_rate']*100:.1f}%）"
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="评测报告离线后处理（污染标记 + 判分修正）")
    p.add_argument("reports", nargs="+", help="评测报告 JSON 路径")
    p.add_argument("--no-llm", action="store_true",
                   help="不调用 judge LLM（仅确定性重判与标记）")
    p.add_argument("--mark", action="append", default=[],
                   metavar="CASE_ID:REASON[:EVIDENCE]",
                   help="人工标记污染（报告内 errors 无法自动判定时；"
                        "evidence 建议提供，作为标注依据写入 contamination_evidence）")
    args = p.parse_args()

    manual_marks: dict[str, tuple[str, str]] = {}
    for m in args.mark:
        parts = m.split(":", 2)
        if len(parts) < 2:
            print(f"--mark 格式错误（应为 CASE_ID:REASON[:EVIDENCE]）：{m}")
            return 1
        cid, reason = parts[0], parts[1]
        evidence = parts[2] if len(parts) > 2 else "人工标注（未提供依据）"
        manual_marks[cid] = (reason, evidence)

    for r in args.reports:
        print(f"处理 {r}:")
        ret = process(r, allow_llm=not args.no_llm, manual_marks=manual_marks)
        if ret != 0:
            return ret
    return 0


if __name__ == "__main__":
    sys.exit(main())
