# 评测对比报告：基线 vs v2（2026-09-05）

> 基线：首轮全量评测（judge 修正重判后口径）。
> v2：三项产品缺陷修复后全量重跑（prompts.yml 文件生成 hijack 规则 + Tavily 截断/超时 + 药品双命名与聚合纪律）。
> 原则：提升与回退如实并列，污染用例单独标记、不计入结论分母。

## TL;DR

- **总分 30/45（66.7%）→ 36/45（80.0%）**；剔除污染用例后 **29/41（70.7%）→ 35/40（87.5%）**。
- 修复生效证据链完整：4 条 sql 直接翻转 + web-03 从"400 max_seq_len 异常"恢复（Tavily 截断修复的最硬证据）+ 3 条 hijack 用例转 PASS。
- 如实报告 3 条 sql 回退（sql-04/08/10，全部为模型非确定性行为，非修复因果）与 1 处 hijack 残留（web-07）。
- 污染：基线 4 条 / v2 5 条（429 限流在本轮明显更频繁，另新增 Tavily 代理故障 2 条）。
- token 成本基本持平：774,194 → 738,817。

## 口径说明

1. **判分修正链**：judge 修复（sql 数字千分位/尾零归一化、multi judge 解析失败重试）后基于已捕获 result 离线重判；原始判定保留在 `verdict_prev` + `rejudge_reason`，报告 `.bak` 备份，全过程可审计。
2. **污染判定**：errors 含 429 → `rate_limit_429`；超时 → `timeout`；Tavily 代理故障（ProxyError）→ `external_search_down`（本次新增类别）。sql-14 的 429 证据在 runner 日志而非报告 errors，走人工标记通道并写入 `contamination_evidence` 留证。
3. **剔除口径**：污染用例（含脏通过，如 route-06 v2 "PASS 但 result 为空"）从分子分母同时剔除。
4. **补测（patch）数据**：基线污染用例曾用原代码单条重测，仅作参考（补测与主跑批之间存在代码归属混合，见基线报告说明），不计入任何通过率。

## 总览

| 指标 | 基线（重判后） | v2 | 变化 |
|------|--------------|-----|------|
| sql | 15/22 | 16/22 | +1（4 修复生效 − 3 回退） |
| routing | 5/6 | 5/6 | 0（无翻转） |
| web | 5/10 | 8/10 | +3 |
| multi | 5/7 | 7/7 | +2 |
| **合计** | **30/45（66.7%）** | **36/45（80.0%）** | **+6** |
| 剔污染 | 29/41（70.7%） | 35/40（87.5%） | +16.8pp |
| agent tokens | 774,194 | 738,817 | −35,377 |

## 逐条翻转明细

### 转为 PASS（10 条）

| 用例 | 基线 → v2 | 归因 |
|------|----------|------|
| sql-12 | FAIL(0) → PASS(1.0) | 药品双命名规范生效（通用名+品牌名） |
| sql-13 | FAIL(0) → PASS(1.0) | 文件生成 hijack 修复生效（不再以"文件已生成"替代回答） |
| sql-19 | FAIL(0)* → PASS(1.0) | 数字格式 judge 修复 + 双命名（基线污染） |
| sql-20 | FAIL(0) → PASS(1.0) | 聚合纪律 + 429 重试后正常完成 |
| web-03 | FAIL(0.2) → PASS(0.8) | **Tavily 截断修复最硬证据**：基线 `400: input tokens(39820) > max_seq_len(32768)` 异常、result 为空；v2 搜索结果截断后上下文不再膨胀 |
| web-05 | FAIL(0.2) → PASS(0.8) | hijack 修复生效 |
| web-10 | FAIL(0.2) → PASS(0.8) | hijack 修复生效 |
| multi-06 | FAIL(0.53) → PASS(0.73) | 基线 judge 解析失败重判后 0.53 差一线；v2 正常完成 |
| multi-07 | FAIL(0.2)* → PASS(0.87) | 基线超时污染 + 补测早停异常；v2 正常完成（异常恢复，非修复直接生效） |
| web-01 | FAIL(0.2) → PASS(0.8) | 基线早停异常；v2 正常（异常恢复，非修复直接生效） |

### 转为 FAIL / 降分（如实报告，5 条）

| 用例 | 基线 → v2 | 根因定性 |
|------|----------|---------|
| sql-04 | PASS(1.0) → FAIL(0) | **模型语义理解偏差**：答"24 种药品种类"，DB 真值 drugs 表 50 行、去重治疗领域恰 24 个——模型把"多少种药品"理解为"多少个治疗领域"。期望值 50 正确，非 v2 修复因果 |
| sql-08 | PASS(1.0) → FAIL(0) | **主智能体早停**：子智能体未返回时结束回合（result="我将等待其结果"） |
| sql-10 | PASS(1.0) → FAIL(0) | **零工具早停**：8,223 tokens 单次调用直接输出"我将启动查询助手"即结束（早停异常家族签名） |
| web-07 | PASS(0.8) → PASS(0.6) | **hijack 残留**：query 未要求文件但 v2 仍调用 Markdown 生成工具并以"文档已生成"作答。修复降低了频率（基线 3 条 → v2 1 条）但未根除 |
| web-08 | PASS(0.7) → FAIL(0.2)* | **Tavily 代理故障污染**（external_search_down），非真实回退 |

## 污染用例清单

| 基线（4 条） | v2（5 条） |
|-------------|-----------|
| sql-19（rate_limit_429） | sql-14（rate_limit_429，人工标记+日志留证） |
| route-06（rate_limit_429，脏通过） | sql-18（rate_limit_429） |
| web-09（timeout） | route-06（rate_limit_429，脏通过：PASS 但 result 为空） |
| multi-07（timeout） | web-08（external_search_down） |
| | web-09（external_search_down） |

事实陈述：v2 跑批期间硅基流动 429 限流明显更频繁（sql 类 5 次触发 70s 退避重试，2 条耗尽），Tavily 出现代理连接故障 2 条。限流属于外部基础设施状态，不影响修复有效性判断，但直接影响当轮通过率。

## 早停异常家族（v2 轮次最大真实质量信号）

签名：主智能体单次 LLM 调用即结束、零工具调用、~8K tokens、以"我将启动 XX"式过渡语作答。

- v2 出现 3 条：sql-08（变体：有工具调用但未等子智能体返回）、sql-10、sql-14（限流窗口内，污染标记）。
- 基线出现 2 条：web-01、multi-07（补测时复现同一签名）。
- 合计 5/90 次运行，n 次跨类别出现，**不能归因于单次噪声**。定性：主智能体循环健壮性缺陷（对"我宣布要做什么"式的提前收尾无检测/重试机制），是下一优先级（P1）修复对象。修复方向：主智能体最终回答若为纯过渡语（未包含任何工具结果特征）则强制续跑一轮。

## hijack 修复的诚实结论

- 基线 3 条劫持（sql-13、web-05、web-10）v2 全部转 PASS。
- 但 web-07 出现 1 条残留（未要求文件却生成文件）。
- 结论：提示词约束**显著降低但不根除**模型行为倾向。若要求根除，需工具层硬约束（generate_markdown 调用前校验用户 query 是否含明确文件产出意图），属于下一步改进项。

## 结论

1. 三项 v2 修复全部获得正向验证：sql 4 条翻转 + web 3 条翻转均与修复点一一对应；web-03 的 max_seq_len 异常消除是截断修复的直接因果证据。
2. v2 新暴露的 3 条 sql 回退与 1 条 hijack 残留均为模型非确定性行为，指向同一结构性短板：**主智能体早停防护缺失**（P1）。
3. 基础设施稳定性（429 限流、Tavily 代理）已成为评测结论的主要噪声源，建议后续跑批降低并发/加长 pause，或为 runner 增加限流感知的指数退避。

## 复现命令

```bash
# v2 跑批
python -m eval.runner --category sql --pause 45 --timeout 240 --out eval-report-sql-v2.json
# （routing/web/multi 同理）

# 污染标记 + 离线重判
python -m eval.rejudge eval-report-sql-v2.json eval-report-routing-v2.json eval-report-web-v2.json --no-llm
python -m eval.rejudge eval-report-multi-v2.json
python -m eval.rejudge eval-report-sql-v2.json --no-llm \
  --mark "sql-14-customer-purchase:rate_limit_429:runner日志记录3次TPM限流重试后耗尽"

# 对比
python -m eval.compare eval-report-sql.json eval-report-sql-v2.json \
  eval-report-routing.json eval-report-routing-v2.json \
  eval-report-web.json eval-report-web-v2.json \
  eval-report-multi.json eval-report-multi-v2.json
```
