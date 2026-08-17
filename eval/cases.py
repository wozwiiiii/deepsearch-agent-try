"""
评测集：20 个固定用例（P3-2 最小版）

设计原则：
1. SQL 类用例有确定性 ground truth——答案可从教学库（drugs 50 / inventory 150 /
   sales_records 100）精确算出，用"期望事实"做子串包含判定，不受模型措辞影响；
2. 路由类用例只验证"派给了正确的子智能体/工具"，不验证答案内容；
3. Web / 多源类用例无确定答案，用 LLM-as-judge 在指定维度上打分。

ground truth 均依据 docker/mysql/mysql.sql 的模拟数据手工核算，标注在 note 字段。
改库数据后必须同步复核 expected_facts，否则评测会误报。
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EvalCase:
    id: str
    query: str
    category: str  # sql | routing | web | multi

    # SQL 类：最终答案必须包含的全部事实（子串包含，大小写不敏感）
    expected_facts: tuple[str, ...] = ()

    # 路由类：期望命中的工具名 / 子智能体名（命中任一即通过）
    expected_tools: tuple[str, ...] = ()
    expected_assistants: tuple[str, ...] = ()

    # Web / 多源类：LLM-as-judge 打分维度
    judge_dims: tuple[str, ...] = ()

    note: str = ""  # ground truth 来源或核验说明


# ---------------------------------------------------------------------------
# A. SQL 事实类（12 个）——确定性 ground truth，子串包含判定
# ---------------------------------------------------------------------------

CASES: list[EvalCase] = [
    # --- 单字段查询（验证主智能体能定位到数据库子智能体并取到正确字段）---
    EvalCase(
        id="sql-01-brand-name",
        query="数据库里布洛芬缓释胶囊的商品名是什么？",
        category="sql",
        expected_facts=("芬必得",),
        note="drugs.drug_id=2，brand_name='芬必得'",
    ),
    EvalCase(
        id="sql-02-generic-name",
        query="立普妥的通用名是什么？",
        category="sql",
        expected_facts=("阿托伐他汀钙片",),
        note="drugs.drug_id=4，generic_name='阿托伐他汀钙片'",
    ),
    EvalCase(
        id="sql-03-therapeutic-area",
        query="格华止属于哪个治疗领域？",
        category="sql",
        expected_facts=("糖尿病",),
        note="drugs.drug_id=3，therapeutic_area='糖尿病'",
    ),

    # --- 计数类（验证 GROUP BY / COUNT 生成）---
    EvalCase(
        id="sql-04-count-drugs",
        query="数据库里一共有多少种药品？",
        category="sql",
        expected_facts=("50",),
        note="drugs 表共 50 行",
    ),
    EvalCase(
        id="sql-05-count-cardio",
        query="数据库里有多少种心血管类的药品？",
        category="sql",
        expected_facts=("6",),
        note="therapeutic_area='心血管'：drug_id 4/9/17/18/22/33 共 6 种",
    ),

    # --- 聚合类（验证 SUM 跨多批次/多记录）---
    EvalCase(
        id="sql-06-inventory-sum",
        query="连花清瘟胶囊的总库存量是多少盒？",
        category="sql",
        expected_facts=("160000",),
        note="inventory drug_id=10：10000+50000+100000=160000（全库最大库存）",
    ),
    EvalCase(
        id="sql-07-sales-sum",
        query="奥司他韦（达菲）的销售额总共是多少？",
        category="sql",
        expected_facts=("700000",),
        note="sales_records drug_id=5：200000+500000=700000",
    ),
    EvalCase(
        id="sql-08-max-inventory",
        query="库存量最大的药品是哪一种？",
        category="sql",
        expected_facts=("连花清瘟",),
        note="按 drug_id 求 SUM(quantity_on_hand) 最大，drug_id=10=160000",
    ),

    # --- JOIN 跨表（验证主表+库存/销售关联）---
    EvalCase(
        id="sql-09-join-sales",
        query="连花清瘟胶囊的销售额总共是多少？",
        category="sql",
        expected_facts=("1200000",),
        note="JOIN drugs+sales_records，drug_id=10：200000+1000000=1200000",
    ),
    EvalCase(
        id="sql-10-customer-qty",
        query="上海华山医院采购了多少盒布洛芬缓释胶囊？",
        category="sql",
        expected_facts=("5000",),
        note="sales_records drug_id=2, customer='上海华山医院'，quantity_sold=5000",
    ),
    EvalCase(
        id="sql-11-expiry-check",
        query="2027 年 3 月过期的药品里，有没有奥美拉唑？",
        category="sql",
        expected_facts=("奥美拉唑",),
        note="inventory drug_id=13 batch MY-250305-13A，expiry_date=2027-03-04",
    ),
    EvalCase(
        id="sql-12-warehouse-listing",
        query="天津一号库里有哪些药品的库存批次？至少列出两种。",
        category="sql",
        expected_facts=("阿莫西林",),  # drug_id=1 有 '天津一号库-A区' 批次
        note="inventory.warehouse_location LIKE '天津一号库%'，含阿莫西林等；任取一种验证",
    ),

    # ---------------------------------------------------------------------------
    # B. 路由类（2 个）——只验证派给了正确子智能体/工具
    # ---------------------------------------------------------------------------
    EvalCase(
        id="route-01-db-routing",
        query="查询数据库里治疗领域为高血压的药品有哪些，只看数据库，不用网络。",
        category="routing",
        expected_assistants=("database_query_agent",),
        expected_tools=("execute_sql_query",),
        note="结构化查询应派给数据库查询助手，命中任一即通过",
    ),
    EvalCase(
        id="route-02-web-routing",
        query="用网络搜索一下奥司他韦治疗流感的最新临床指南，不要查数据库。",
        category="routing",
        expected_assistants=("network_search_agent",),
        note="时效性开放问题应派给网络搜索助手",
    ),

    # ---------------------------------------------------------------------------
    # C. Web 类（4 个）——LLM-as-judge 打分（groundedness / completeness）
    # ---------------------------------------------------------------------------
    EvalCase(
        id="web-01-mechanism",
        query="用网络资料简要说明奥司他韦的作用机制。",
        category="web",
        judge_dims=("groundedness", "completeness"),
        note="应提到神经氨酸酶抑制；judge 评有依据、覆盖要点",
    ),
    EvalCase(
        id="web-02-clinical",
        query="搜索连花清瘟胶囊的临床应用场景。",
        category="web",
        judge_dims=("groundedness", "completeness"),
        note="应提到流感/热毒袭肺证；judge 评有依据、覆盖要点",
    ),
    EvalCase(
        id="web-03-drug-class",
        query="查询并解释 SGLT2 抑制剂（如达格列净）的降糖机制。",
        category="web",
        judge_dims=("groundedness", "completeness"),
        note="应提到近端肾小管钠-葡萄糖共转运蛋白抑制、促进尿糖排泄",
    ),
    EvalCase(
        id="web-04-safety",
        query="搜索阿司匹林肠溶片长期低剂量使用的注意事项。",
        category="web",
        judge_dims=("groundedness", "completeness"),
        note="应提到出血风险、胃肠道反应等",
    ),

    # ---------------------------------------------------------------------------
    # D. 多源类（2 个）——同时用数据库 + 网络，judge 综合评分
    # ---------------------------------------------------------------------------
    EvalCase(
        id="multi-01-cardio-report",
        query="结合数据库和网络，整理一份心血管类药品的库存概况与临床用途说明。"
        "先从数据库查心血管药品清单和库存，再用网络补充其中至少两种药的临床用途。",
        category="multi",
        judge_dims=("groundedness", "completeness", "data_use"),
        note="应同时出现数据库事实（如 6 种/具体药名）和网络内容（临床用途）",
    ),
    EvalCase(
        id="multi-02-diabetes-summary",
        query="数据库里糖尿病类药品有哪些？再用网络查一下其中降糖药的代表机制，"
        "汇总成一段说明。",
        category="multi",
        judge_dims=("groundedness", "completeness", "data_use"),
        note="数据库事实：5 种糖尿病药（二甲双胍/格列美脲/达格列净/甘精胰岛素/阿卡波糖）",
    ),
]


def by_category(category: str) -> list[EvalCase]:
    return [c for c in CASES if c.category == category]


if __name__ == "__main__":
    # 自检：用例数与分布
    from collections import Counter

    counts = Counter(c.category for c in CASES)
    print(f"共 {len(CASES)} 个用例：{dict(counts)}")
    assert len(CASES) == 20, f"预期 20 个用例，实际 {len(CASES)}"
    for c in CASES:
        if c.category == "sql":
            assert c.expected_facts, f"{c.id} 缺 expected_facts"
        elif c.category == "routing":
            assert c.expected_tools or c.expected_assistants, f"{c.id} 缺路由期望"
        elif c.category in ("web", "multi"):
            assert c.judge_dims, f"{c.id} 缺 judge_dims"
    print("用例自检通过")
