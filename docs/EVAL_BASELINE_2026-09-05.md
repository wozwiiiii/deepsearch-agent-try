# 评测基线报告（首轮全量）— 2026-09-05

> 项目至今第一份真实基线。此前评测集 45 条从未完整跑过（WORK_STATUS P0 阻塞项）。
> 本报告所有数字来自真实运行日志与 JSON 报告（`eval-report-{sql,routing,web,multi}.json`），
> 判定修正全程可审计（原始值保留在 `verdict_prev`，原始报告备份为 `*.json.bak`）。

## 一、运行环境与成本

| 项 | 值 |
|------|------|
| 主模型 | `Qwen/Qwen2.5-32B-Instruct`（硅基流动，¥1.26/M tokens） |
| 搜索 | Tavily（免费档，1000 credits/月，实测消耗估算 <10%） |
| 数据库 | 本地 MySQL（deepsearch_db：drugs / inventory / sales_records） |
| 用例数 | 45（sql 22 / routing 6 / web 10 / multi 7） |
| 墙钟时间 | 1h25m51s（含限流退避） |
| agent 侧 token | **774,194**（judge 判分开销未计入） |
| 实际成本 | **≈ ¥0.98**（预算 ¥10，占用 9.8%） |

分类别 token（含每调用约 8K 的系统提示词+工具 schema 固定开销）：

| 类别 | n | 平均 | 最大 | 说明 |
|------|---|------|------|------|
| sql | 22 | 16,822 | 33,162 | 单表/聚合/JOIN |
| routing | 6 | 13,832 | 17,052 | 工具/子智能体路由 |
| web | 10 | 15,687 | 37,657 | 搜索+作答 |
| multi | 7 | 23,462 | 47,676 | 多源融合，token 显著更高 |

## 二、总成绩

| 口径 | 通过率 | 说明 |
|------|--------|------|
| **全量 45 条** | **30/45（66.7%）** | judge bug 修正后 |
| 剔除 4 条污染用例 | **29/41（70.7%）** | 429/超时未获公平评测的用例单列 |

| 类别 | 通过 | 率 | 污染 | 污染用例 |
|------|------|-----|------|----------|
| sql | 15/22 | 68.2% | 1 | sql-19（429） |
| routing | 5/6 | 83.3% | 1 | route-06（429） |
| web | 5/10 | 50.0% | 1 | web-09（Tavily 20s 超时） |
| multi | 5/7 | 71.4% | 1 | multi-07（240s 总超时） |

## 三、数据可信度处理（判分修正记录）

首轮原始判分 27/45，有三处 judge 自身缺陷，修复后离线重判（零 API 重跑成本）：

1. **routing 匹配方向反**（首跑即暴露，此前会话已修）：monitor 上报的是中文名记录
   （"数据库查询助手"），期望值是标识符，旧实现方向写反导致 6 条全 0 分。
   修正后 5/6（route-04 为真 FAIL）。
2. **sql 数字千分位/小数尾零**：模型把 160000 写成 "160,000"、"1,200,000.00"，
   朴素子串匹配误判。已对照 MySQL 真值核实模型答案正确，修正后 sql-06/07/09 转 PASS。
   归一化只作用于数字相邻字符（`judge_sql` 内 `_normalize_for_match`），配 4 条单测。
3. **multi-06 judge LLM 输出 JSON 解析失败兜底 0 分**：重试一次得 0.53（仍 <0.6 不通过，
   但分数真实化：边缘失误而非全错）。

全部修正保留原值：用例级 `verdict_prev` + `rejudge_reason`；报告级 `note` + `rejudged_at`；
重判工具为可复现脚本 `eval/rejudge.py`（测试 `tests/test_eval_judge.py` 9 条全绿）。

**污染判定标准**（`eval/rejudge.py`）：错误信息含 429 → `rate_limit_429`；含 超时/timed out →
`timeout`。这些用例因外部资源故障未完成公平评测，不计入通过率分母的分析结论，冷却后可补测。

## 四、FAIL 明细（实事求是分类）

### 4.1 真实模型/Agent 缺陷（9 条，修复优先级见第五节）

| 用例 | 现象 | 初步归因 |
|------|------|----------|
| sql-18 | 答总库存 94,000/总销售 270,000，真值 47,000/90,000 | 多批次聚合时数字幻觉（94,000=47,000×2） |
| sql-20 | 最小库存答"博叶"且自相矛盾，真值为替诺福韦(500) | 聚合+排序稳定性差 |
| sql-21 | 字段名（治疗领域）与预期不符后停下反问用户 | schema 理解弱，缺"探查列值再查"策略 |
| sql-12 | 只报品牌名"阿莫仙"，未提通用名"阿莫西林" | 答案精确度：该批次通用名即阿莫西林胶囊 |
| sql-13 | 把区域排名写成 Markdown 文件，未在回答中给结论 | 任务驱动 hijack：文件生成工具喧宾夺主 |
| sql-14 | 只列药品 ID 与分项金额，未汇总 45,200 | 多行聚合未完成 |
| route-04 | 查库后未调用 Markdown 生成工具，直接文本作答 | 工具编排缺陷（已确认非判分问题） |
| web-05/web-10 | 同 sql-13：生成文件而非作答 | 同上 |
| multi-06 | 0.53 分（边缘）：报告生成了但融合深度不足 | 长流程质量衰减 |

### 4.2 真实边界发现（2 条，属产品要修的问题而非噪声）

- **web-03：上下文硬限**。Qwen2.5-32B 上下文 32768，累积搜索结果后输入达 39,820 tokens
  报 400。长搜索场景需要结果截断/压缩策略。
- **web-09：Tavily 20s 超时过短**（同时计入污染）。搜索工具超时配置不足以覆盖慢查询。

### 4.3 观察到的异常

- **web-01：空响应**。消耗 8,061 tokens 但最终回答为空、无错误记录（与 7B 实验期
  空响应同签名，32B 下偶发）。值得补测一次确认复现性。
- multi-07 的 240s 超时无法区分"真实慢"与"限流重试吃掉预算"，补测时建议单独观察。

## 五、改进方向（按 ROI 排序）

1. **P0 修"文件生成 hijack"**（影响 sql-13/web-05/web-10 至少 3 条）：
   提示词明确"仅在用户要求产出文件时才生成文件，否则直接作答"。
2. **P0 搜索结果截断/压缩**（web-03）：按 token 预算截断搜索结果再入上下文。
3. **P1 聚合类 SQL 稳定性**（sql-18/14/20）：提示词强化"数字必须来自单条 SQL 聚合结果，
   禁止心算/拼接"；考虑给 execute_sql_query 结果加复核步骤。
4. **P1 route-04 工具编排**：查库→生成周报的链路中 Markdown 工具未被选择，排查
   工具描述对"周报"意图的覆盖。
5. **P2 Tavily 超时上调**（web-09）；web-01 空响应补测确认。

## 六、复现

```bash
# 全量（限速防 429：被拒请求也占限流窗口，pause 需≥45s）
python -m eval.runner --category sql   --pause 45 --timeout 240 --out eval-report-sql.json
python -m eval.runner --category routing --pause 45 --timeout 240 --out eval-report-routing.json
python -m eval.runner --category web   --pause 45 --timeout 240 --out eval-report-web.json
python -m eval.runner --category multi --pause 45 --timeout 240 --out eval-report-multi.json

# 离线后处理（污染标记 + judge 修正重判，零 Agent 重跑成本）
python -m eval.rejudge eval-report-sql.json eval-report-routing.json eval-report-web.json --no-llm
python -m eval.rejudge eval-report-multi.json
```

注意：固定 thread_id 重跑会触发 warm-start（模型凭 checkpoint 会话记忆直接作答、不调工具），
runner 已强制每次尝试全新 thread_id，请勿改回固定 id。

---
*数据来源：2026-09-05 实测。判定修正链路：judge.py → rejudge.py → 报告 verdict_prev/.bak，全程可审计。*
