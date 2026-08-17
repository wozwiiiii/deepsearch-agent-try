"""
评测运行器：对每个用例跑一遍真实 Agent，捕获结果与工具调用，再判分。

使用方式（需真实 OPENAI_API_KEY / TAVILY_API_KEY / MYSQL_* / RAGFLOW_*）：

    # 跑全部 20 个用例
    python -m eval.runner

    # 只跑 SQL 类
    python -m eval.runner --category sql

    # 只跑一个用例（调试用）
    python -m eval.runner --case sql-06-inventory-sum

    # LLM-as-judge 维度多次取均值（缓解概率波动），结果写文件
    python -m eval.runner --runs 3 --out eval-report.json

设计要点：
- 直接调用 run_deep_agent（异步），不启 HTTP 服务；
- monkeypatch app.api.monitor 的 report_* 方法捕获最终结果、工具调用、子智能体路由
  （工具在调用时查 monitor 的属性，所以替换属性即可生效）；
- 每个 case 用独立 thread_id（eval-{id}）+ user_id=eval，会话目录隔离到
  output/user_eval/session_eval-{id}，跑完不自动清理（便于人工核查产物）。
"""

import argparse
import asyncio
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

from app.api import monitor as monitor_mod
from app.api.monitor import monitor
from app.agent.main_agent import run_deep_agent

from eval.cases import CASES, EvalCase
from eval.judge import judge_routing, judge_sql, judge_with_llm


# ---------------------------------------------------------------------------
# 捕获：临时替换 monitor 的 report_* 方法，把事件收进 dict
# ---------------------------------------------------------------------------

def _capture_for_case() -> dict:
    """返回 (capture, restore)：capture 是结果容器，restore 恢复原方法"""
    box = {"result": None, "tools": [], "assistants": [], "errors": []}

    orig = {
        "report_task_result": monitor.report_task_result,
        "report_tool": monitor.report_tool,
        "report_assistant": monitor.report_assistant,
        "report_error": monitor.report_error,
    }

    def cap_result(result):  # 参数名与原方法一致
        box["result"] = result

    def cap_tool(tool_name, args=None):
        box["tools"].append(tool_name)

    def cap_assistant(assistant_name, args=None):
        box["assistants"].append(assistant_name)

    def cap_error(message):
        box["errors"].append(message)

    monitor.report_task_result = cap_result
    monitor.report_tool = cap_tool
    monitor.report_assistant = cap_assistant
    monitor.report_error = cap_error

    def restore():
        for k, v in orig.items():
            setattr(monitor, k, v)

    return box, restore


# ---------------------------------------------------------------------------
# 单个用例执行
# ---------------------------------------------------------------------------

async def run_one(case: EvalCase, timeout: float = 180.0) -> dict:
    """跑一个用例，返回执行结果（含捕获到的工具/结果与判分）"""
    box, restore = _capture_for_case()
    thread_id = f"eval-{case.id}"
    started = datetime.now().isoformat()

    try:
        await asyncio.wait_for(
            run_deep_agent(case.query, thread_id, user_id="eval"),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        box["errors"].append(f"超时（{timeout}s）")
    except Exception as e:
        box["errors"].append(f"执行异常: {e}\n{traceback.format_exc()}")
    finally:
        restore()

    return {
        "id": case.id,
        "category": case.category,
        "query": case.query,
        "started_at": started,
        "result": box["result"],
        "tools": box["tools"],
        "assistants": box["assistants"],
        "errors": box["errors"],
    }


# ---------------------------------------------------------------------------
# 判分
# ---------------------------------------------------------------------------

def score_one(case: EvalCase, run: dict) -> dict:
    """根据 category 选判分器，返回 {pass, score, detail}"""
    if case.category == "sql":
        return judge_sql(run.get("result") or "", case.expected_facts)
    if case.category == "routing":
        return judge_routing(run.get("tools", []), run.get("assistants", []),
                             case.expected_tools, case.expected_assistants)
    if case.category in ("web", "multi"):
        return judge_with_llm(case.query, run.get("result") or "", case.judge_dims)
    return {"pass": False, "score": 0.0, "detail": {"error": "未知 category"}}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

async def main_async(args) -> int:
    cases = select_cases(args)
    if not cases:
        print("没有匹配的用例", file=sys.stderr)
        return 1

    print(f"开始评测：{len(cases)} 个用例（每个最多 {args.timeout}s）\n")

    report = {
        "generated_at": datetime.now().isoformat(),
        "total": len(cases),
        "cases": [],
    }
    passed = 0

    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case.id} ({case.category}) ...", flush=True)
        run = await run_one(case, timeout=args.timeout)

        # LLM-as-judge 类支持多次取均值
        if case.category in ("web", "multi") and args.runs > 1:
            scores = [score_one(case, run)]
            for _ in range(args.runs - 1):
                scores.append(score_one(case, run))
            avg = sum(s["score"] for s in scores) / len(scores)
            verdict = {
                "pass": avg >= 0.6,  # 0-1 归一后，均值>=0.6（即 3/5）记通过
                "score": round(avg, 2),
                "detail": {"runs": scores},
            }
        else:
            verdict = score_one(case, run)

        run["verdict"] = verdict
        report["cases"].append(run)
        if verdict["pass"]:
            passed += 1
        status = "PASS" if verdict["pass"] else "FAIL"
        print(f"    -> {status}  score={verdict['score']}"
              f"  tools={run['tools']}  assistants={run['assistants']}")
        if run["errors"]:
            print(f"    errors: {run['errors']}")

    report["passed"] = passed
    report["pass_rate"] = round(passed / len(cases), 3)

    print(f"\n{'='*50}")
    print(f"通过 {passed}/{len(cases)}（{report['pass_rate']*100:.1f}%）")

    # 按类别细分
    by_cat: dict[str, list[float]] = {}
    for c in report["cases"]:
        by_cat.setdefault(c["category"], []).append(c["verdict"]["score"])
    print("\n按类别平均分：")
    for cat, scores in by_cat.items():
        avg = sum(scores) / len(scores)
        print(f"  {cat:8} {avg:.2f}  (n={len(scores)})")

    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n详细报告已写入 {args.out}")

    return 0 if passed == len(cases) else 2


def select_cases(args) -> list[EvalCase]:
    if args.case:
        return [c for c in CASES if c.id == args.case]
    if args.category:
        return [c for c in CASES if c.category == args.category]
    return list(CASES)


def main():
    p = argparse.ArgumentParser(description="deepsearch-agents 评测运行器")
    p.add_argument("--case", help="只跑指定 id 的用例")
    p.add_argument("--category", choices=["sql", "routing", "web", "multi"],
                   help="只跑某一类用例")
    p.add_argument("--runs", type=int, default=1,
                   help="LLM-as-judge 重复次数（取均值），默认 1")
    p.add_argument("--timeout", type=float, default=180.0,
                   help="单用例超时秒数，默认 180")
    p.add_argument("--out", help="JSON 报告输出路径")
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
