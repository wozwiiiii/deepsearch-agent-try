"""评测报告对比工具：基线 vs 迭代版，逐类/逐条呈现差异

用法（成对传入基线报告与迭代报告）：
    python -m eval.compare eval-report-sql.json eval-report-sql-v2.json \
                          eval-report-routing.json eval-report-routing-v2.json

输出：
- 每对报告：通过数变化、token 变化、判分翻转明细（FAIL→PASS / PASS→FAIL /
  LLM-as-judge 的分数变化），污染用例单独标注；
- 末尾汇总：总通过率对比。

设计原则：只做确定性统计，不做任何美化；回退（PASS→FAIL）与提升同样显式输出。
"""

import argparse
import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def fmt_verdict(case: dict) -> str:
    v = case.get("verdict", {})
    tag = "*" if case.get("contaminated") else ""
    return f"{'PASS' if v.get('pass') else 'FAIL'}({v.get('score')}){tag}"


def compare_pair(base_path: str, v2_path: str) -> None:
    base, v2 = load(base_path), load(v2_path)
    b_cases = {c["id"]: c for c in base["cases"]}
    v2_cases = {c["id"]: c for c in v2["cases"]}
    common_ids = [c["id"] for c in base["cases"] if c["id"] in v2_cases]

    b_pass = sum(1 for i in common_ids if b_cases[i]["verdict"]["pass"])
    v2_pass = sum(1 for i in common_ids if v2_cases[i]["verdict"]["pass"])
    b_tok = sum(b_cases[i].get("tokens_used", 0) for i in common_ids)
    v2_tok = sum(v2_cases[i].get("tokens_used", 0) for i in common_ids)
    cat = common_ids[0].split("-")[0] if common_ids else "?"

    print(f"\n{'='*60}")
    print(f"[{cat}] {Path(base_path).name} vs {Path(v2_path).name}")
    print(f"  通过: {b_pass}/{len(common_ids)} -> {v2_pass}/{len(common_ids)}"
          f"   token: {b_tok:,} -> {v2_tok:,}")

    flips = []
    for cid in common_ids:
        b, v = b_cases[cid], v2_cases[cid]
        bp, vp = b["verdict"]["pass"], v["verdict"]["pass"]
        bs, vs = b["verdict"].get("score"), v["verdict"].get("score")
        if bp != vp:
            direction = "PASS -> FAIL" if bp and not vp else "FAIL -> PASS"
            flips.append(f"  [翻转] {cid}: {direction}  "
                         f"({fmt_verdict(b)} -> {fmt_verdict(v)})")
        elif not bp and (bs != vs):
            flips.append(f"  [分数] {cid}: {bs} -> {vs}（均 FAIL）")
        if v.get("contaminated") and not b.get("contaminated"):
            flips.append(f"  [污染] {cid}: v2 运行出现污染"
                         f"（{v.get('contamination_reason')}），结论需谨慎")
    if flips:
        print("\n".join(flips))
    else:
        print("  （无判分翻转）")


def main() -> int:
    p = argparse.ArgumentParser(description="评测报告成对对比")
    p.add_argument("pairs", nargs="+", metavar="REPORT",
                   help="基线报告与迭代报告成对给出（偶数个）")
    args = p.parse_args()
    if len(args.pairs) % 2 != 0:
        print("报告必须成对给出（基线 迭代 基线 迭代 ...）", file=sys.stderr)
        return 1
    for i in range(0, len(args.pairs), 2):
        compare_pair(args.pairs[i], args.pairs[i + 1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
