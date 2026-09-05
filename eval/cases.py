"""
评测集：45 个固定用例（P3-2 增量扩量版）

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
# A. SQL 事实类（22 个）——确定性 ground truth，子串包含判定
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

    # --- 区域聚合（GROUP BY region）---
    EvalCase(
        id="sql-13-region-ranking",
        query="按销售区域统计销售总额，销售额最高的区域是哪个？",
        category="sql",
        expected_facts=("华东区",),
        note="sales_records GROUP BY region 求 SUM(total_amount)：华东区=1032460 最高，"
        "全国=1020280，华北区=917610",
    ),
    # --- 客户维度（customer_name 聚合）---
    EvalCase(
        id="sql-14-customer-purchase",
        query="广州呼吸健康研究院采购了哪些药品？采购总额是多少？",
        category="sql",
        expected_facts=("复方甲氧那明", "布地奈德", "45200"),
        note="sales_records customer_name='广州呼吸健康研究院'："
        "复方甲氧那明胶囊 14400 + 布地奈德福莫特罗吸入粉雾剂 30800 = 45200",
    ),
    # --- 效期预警（expiry_date 范围统计）---
    EvalCase(
        id="sql-15-expiry-alert",
        query="数据库里 2027 年上半年（1月至6月）到期的库存批次一共有多少条？",
        category="sql",
        expected_facts=("60",),
        note="inventory expiry_date 2027-01-01~2027-06-30：drugs1-10 各2批(01-01/06-14)=20，"
        "drugs11-30 各1批(03-04)=20，drugs31-50 各1批(04-09)=20，共60批",
    ),
    # --- 剂型统计（dosage_form 计数）---
    EvalCase(
        id="sql-16-dosage-form",
        query="数据库里剂型为胶囊剂的药品有多少种？",
        category="sql",
        expected_facts=("8",),
        note="drugs.dosage_form='胶囊剂'：drug_id 1/2/5/10/13/23/30/44 共8种",
    ),
    # --- 金额计算（单药全年销售总额）---
    EvalCase(
        id="sql-17-sales-total",
        query="阿莫西林胶囊的销售总额是多少？",
        category="sql",
        expected_facts=("17250",),
        note="sales_records drug_id=1：5000+12250=17250",
    ),
    # --- 三表 JOIN（库存 vs 销售）---
    EvalCase(
        id="sql-18-join-inventory-sales",
        query="布洛芬缓释胶囊的总库存量和总销售额分别是多少？",
        category="sql",
        expected_facts=("47000", "90000"),
        note="三表JOIN drug_id=2：inventory 2000+15000+30000=47000；"
        "sales_records 15000+75000=90000",
    ),
    # --- 单价极值（MAX unit_price）---
    EvalCase(
        id="sql-19-max-unit-price",
        query="数据库里销售单价最高的药品是哪一种？",
        category="sql",
        expected_facts=("替诺福韦",),
        note="sales_records 最大 unit_price=520.00 → drug_id=42 富马酸替诺福韦二吡呋酯片（韦瑞德）",
    ),
    # --- 库存极值（MIN SUM(quantity_on_hand)）---
    EvalCase(
        id="sql-20-min-inventory",
        query="库存总量最低的药品是哪一种？",
        category="sql",
        expected_facts=("替诺福韦",),
        note="inventory 按 drug_id 求 SUM(quantity_on_hand) 最小：drug_id=42=500+1200+2000=3700",
    ),
    # --- 治疗领域库存聚合（高血压）---
    EvalCase(
        id="sql-21-area-inventory",
        query="数据库里治疗领域为高血压的药品，总库存量是多少？",
        category="sql",
        expected_facts=("88500",),
        note="therapeutic_area='高血压'：drug_id 8/15/16/23，"
        "库存 9500+25500+33000+20500=88500",
    ),
    # --- 批准文号查询 ---
    EvalCase(
        id="sql-22-approval-number",
        query="批准文号为“国药准字H20100345”的药品是哪一种？",
        category="sql",
        expected_facts=("硝苯地平",),
        note="drugs.approval_number='国药准字H20100345' → drug_id=8 硝苯地平控释片（拜新同）",
    ),

    # ---------------------------------------------------------------------------
    # B. 路由类（6 个）——只验证派给了正确子智能体/工具
    # ---------------------------------------------------------------------------
    EvalCase(
        id="route-01-db-routing",
        query="查询数据库里治疗领域为高血压的药品有哪些，只看数据库，不用网络。",
        category="routing",
        # 期望值用 monitor 实际记录名体系：子智能体注册名（prompts.sub_agents_content）
        # 是中文；工具上报名带中文前缀但含函数名，期望函数名做子串即可命中
        expected_assistants=("数据库查询助手",),
        expected_tools=("execute_sql_query",),
        note="结构化查询应派给数据库查询助手，命中任一即通过",
    ),
    EvalCase(
        id="route-02-web-routing",
        query="用网络搜索一下奥司他韦治疗流感的最新临床指南，不要查数据库。",
        category="routing",
        expected_assistants=("网络搜索助手",),
        note="时效性开放问题应派给网络搜索助手",
    ),
    EvalCase(
        id="route-03-knowledge-base",
        query="查询公司内部知识库里关于药品仓储管理制度的规定内容。",
        category="routing",
        expected_assistants=("RAGFlow助手",),
        note="企业内部文档/制度问答应派给知识库助手（RAGFlow）",
    ),
    EvalCase(
        id="route-04-markdown-gen",
        query="根据查到的库存数据，生成一份 Markdown 格式的库存周报。",
        category="routing",
        # markdown_tools 上报名即 "Markdown文档生成工具"（无函数名后缀）
        expected_tools=("Markdown文档生成工具",),
        note="生成 Markdown 报告应命中 generate_markdown 工具",
    ),
    EvalCase(
        id="route-05-negative-routing",
        query="不要查数据库，只用网络搜索最新的奥司他韦耐药性研究进展。",
        category="routing",
        expected_assistants=("网络搜索助手",),
        note="明确排除数据库、只上网，应派给网络搜索助手（负向路由）",
    ),
    EvalCase(
        id="route-06-db-web-combo",
        query="先查数据库里达格列净的库存和销售情况，再用网络补充它的临床研究新进展。",
        category="routing",
        expected_assistants=("数据库查询助手", "网络搜索助手"),
        note="数据库+网络双要求，命中任一子智能体即通过",
    ),

    # ---------------------------------------------------------------------------
    # C. Web 类（10 个）——LLM-as-judge 打分（groundedness / completeness）
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
    EvalCase(
        id="web-05-metformin",
        query="搜索二甲双胍的用药注意事项和常见不良反应。",
        category="web",
        judge_dims=("groundedness", "completeness"),
        note="应覆盖胃肠道反应、乳酸酸中毒风险、肾功能监测、造影剂前停药等要点",
    ),
    EvalCase(
        id="web-06-statin-liver",
        query="搜索他汀类药物（如阿托伐他汀）的肝酶监测与肌病风险注意事项。",
        category="web",
        judge_dims=("groundedness", "completeness"),
        note="应覆盖转氨酶监测、肌痛/横纹肌溶解风险、用药前肝功能评估",
    ),
    EvalCase(
        id="web-07-ppi-risk",
        query="搜索质子泵抑制剂（如奥美拉唑）长期使用的潜在风险。",
        category="web",
        judge_dims=("groundedness", "completeness"),
        note="应覆盖骨折风险、低镁血症、维生素B12缺乏、艰难梭菌感染等长期风险",
    ),
    EvalCase(
        id="web-08-sglt2-benefit",
        query="搜索 SGLT2 抑制剂（如达格列净）的心血管与肾脏保护获益。",
        category="web",
        judge_dims=("groundedness", "completeness"),
        note="应覆盖心衰住院风险降低、肾脏终点获益、合并心肾疾病人群",
    ),
    EvalCase(
        id="web-09-dual-antiplatelet",
        query="搜索阿司匹林与氯吡格雷双联抗血小板治疗的应用场景与出血风险。",
        category="web",
        judge_dims=("groundedness", "completeness"),
        note="应覆盖急性冠脉综合征/PCI 术后双抗疗程、出血风险权衡",
    ),
    EvalCase(
        id="web-10-cold-antibiotic",
        query="搜索普通感冒时抗菌药的合理使用原则，如何避免滥用抗生素。",
        category="web",
        judge_dims=("groundedness", "completeness"),
        note="应覆盖病毒性感冒无需抗菌药、细菌感染指征、耐药性防控",
    ),

    # ---------------------------------------------------------------------------
    # D. 多源类（7 个）——同时用数据库 + 网络，judge 综合评分
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
    EvalCase(
        id="multi-03-hypertension",
        query="先从数据库查治疗领域为高血压的药品清单及库存，再用网络补充其中至少一种药的降压机制，"
        "汇总成一段说明。",
        category="multi",
        judge_dims=("groundedness", "completeness", "data_use"),
        note="数据库事实：4种高血压药（硝苯地平/氯沙坦/氨氯地平/缬沙坦）+库存88500；网络补充降压机制",
    ),
    EvalCase(
        id="multi-04-antibiotic",
        query="先从数据库查抗生素类药品的库存情况，再用网络补充其中至少一种抗生素的临床适应症与耐药注意。",
        category="multi",
        judge_dims=("groundedness", "completeness", "data_use"),
        note="数据库事实：抗生素药（阿莫西林/头孢曲松/头孢克肟）库存；网络补充适应症与耐药",
    ),
    EvalCase(
        id="multi-05-digestive",
        query="先从数据库查消化系统类药品的销售情况，再用网络补充其中质子泵抑制剂的临床用药指南要点。",
        category="multi",
        judge_dims=("groundedness", "completeness", "data_use"),
        note="数据库事实：消化系统药销售（蒙脱石散/奥美拉唑/雷贝拉唑/乳果糖）；网络补充用药指南",
    ),
    EvalCase(
        id="multi-06-region-sales",
        query="先从数据库查华东区的销售记录与销售总额，再用网络补充医药零售市场趋势，"
        "汇总该区域的销售表现分析。",
        category="multi",
        judge_dims=("groundedness", "completeness", "data_use"),
        note="数据库事实：华东区销售总额1032460（各药 total_amount 求和）；网络补充市场分析",
    ),
    EvalCase(
        id="multi-07-expiry-management",
        query="先从数据库查 2027 年上半年到期的库存批次及涉及的药品，再用网络补充药品效期与库存管理最佳实践。",
        category="multi",
        judge_dims=("groundedness", "completeness", "data_use"),
        note="数据库事实：2027上半年到期批次共60条（覆盖全部50种药）；网络补充效期/库存管理建议",
    ),
]


def by_category(category: str) -> list[EvalCase]:
    return [c for c in CASES if c.category == category]


if __name__ == "__main__":
    # 自检：用例数、分布与结构完整性（CI 中作为评测集的"语法检查"运行）
    from collections import Counter

    counts = Counter(c.category for c in CASES)
    print(f"共 {len(CASES)} 个用例：{dict(counts)}")
    assert len(CASES) == 45, f"预期 45 个用例，实际 {len(CASES)}"

    # 重复 id 会让 runner --case 筛选命中多条、报告无法区分，必须唯一
    dup_ids = [i for i, n in Counter(c.id for c in CASES).items() if n > 1]
    assert not dup_ids, f"存在重复用例 id: {dup_ids}"

    for c in CASES:
        if c.category == "sql":
            assert c.expected_facts, f"{c.id} 缺 expected_facts"
        elif c.category == "routing":
            assert c.expected_tools or c.expected_assistants, f"{c.id} 缺路由期望"
        elif c.category in ("web", "multi"):
            assert c.judge_dims, f"{c.id} 缺 judge_dims"
    print("用例自检通过")
