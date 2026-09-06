# 评测集（P3-2 最小版）

> 20 个固定用例 + 确定性判分 + LLM-as-judge，把 Agent 的"效果"从"感觉能用"变成可量化的数字。
> 这是 Agent 项目区别于 demo 的核心证据：改提示词/换模型/调检索参数后，能说清"变好了还是变差了"。

## 目录结构

```
eval/
├── cases.py     # 20 个用例与 ground truth（核心 IP）
├── judge.py     # 判分器：SQL 子串包含 / 路由命中 / LLM-as-judge
├── runner.py    # 运行器：调 run_deep_agent，monkeypatch monitor 捕获，输出报告
└── README.md    # 本文件
```

## 用例分布

| 类别 | 数量 | 判分方式 | 说明 |
|------|------|----------|------|
| sql | 12 | 子串包含（确定性） | 答案可从教学库精确算出，不受模型措辞影响 |
| routing | 2 | 工具/子智能体命中 | 只验证派给了正确的子智能体 |
| web | 4 | LLM-as-judge | groundedness / completeness 维度打 1-5 分 |
| multi | 2 | LLM-as-judge | 多源综合，含 data_use 维度（是否真用了数据库事实） |

ground truth 全部依据 `docker/mysql/mysql.sql` 手工核算，标注在每条用例的 `note` 字段。**改库数据后必须复核 `expected_facts`，否则会误报。**

## 运行（需真实服务）

评测要跑真实 Agent，因此需要 `.env` 配齐：

```
OPENAI_API_KEY=...      # 实际是阿里云 DashScope 兼容接口
TAVILY_API_KEY=...
MYSQL_HOST/PORT/USER/PASSWORD/DATABASE=...
（RAGFLOW_* 如有用到）
```

先确认 MySQL 已起且导入了教学数据：

```bash
cd docker && docker compose up -d mysql
# 首次启动会自动执行 01-mysql.sql + 02-create-readonly-user.sh
```

然后跑评测（在 deepsearch-agents/ 下）：

```bash
# 全部 20 个
python -m eval.runner

# 只跑 SQL 类（最快，不依赖网络/LLM judge）
python -m eval.runner --category sql

# 调试单个用例
python -m eval.runner --case sql-06-inventory-sum

# LLM judge 多次取均值（缓解概率波动），结果写文件
python -m eval.runner --category web --runs 3 --out eval-report.json
```

## 输出示例

```
[1/20] sql-01-brand-name (sql) ...
    -> PASS  score=1.0  tools=['execute_sql_query']  assistants=['database_query_agent']
...
==================================================
通过 17/20（85.0%）

按类别平均分：
  sql       0.92  (n=12)
  routing   1.00  (n=2)
  web       0.74  (n=4)
  multi     0.65  (n=2)
```

`--out` 会写完整 JSON：每个用例的 query / 捕获到的 result / tools / assistants / errors / verdict（pass + score + detail）。

## 它衡量什么、不衡量什么

**衡量**：
- 路由正确性（派对子智能体）
- SQL 事实准确性（答案含正确数字/名称）
- 生成质量（web/multi 的有据性、完整性）

**不衡量**：
- 速度/成本（需要另行埋点 token 与耗时，见 docs/status/PRODUCTION_NOTES P1-4）
- 鲁棒性（边界输入、对抗提示注入——需要专门的对抗集）
- 长会话记忆（需要多轮用例，最小版未覆盖）

## LLM-as-judge 的已知偏差与缓解（面试可讲）

| 偏差 | 表现 | 缓解 |
|------|------|------|
| 长度偏好 | 长答案易得高分 | 提示词显式要求"不以长度论高低" |
| 位置偏好 | 答案在前/在后影响分 | 要求逐维度先给理由再给分 |
| 概率波动 | 同一答案多次打分不同 | `--runs N` 取均值 |

仍是概率打分，不能当真值用——SQL 类的确定性判定才是硬证据。**讲评测时主动说"LLM judge 有偏差、所以我用 SQL 确定性用例做主力、LLM judge 只补开放题"——这比假装 LLM 打分完美更可信。**

## 局限与下一步

- 20 条是最小可用品；扩到 50 条 + 加对抗集后才能真正接 CI 做回归门槛；
- Web/RAG 用例的 ground truth 依赖外部网页时效，跑前应抽检是否仍可达；
- 评测会真实消耗 LLM/Tavily 配额，跑全量约一次几十次模型调用。

## 自检（无需真实服务）

```bash
python -m eval.cases   # 校验 20 个用例结构完整、ground truth 齐备
```
