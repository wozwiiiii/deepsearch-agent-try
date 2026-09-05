# 项目使用、测试与纠错指南

> 面向"想动手把项目跑起来、改一改、排错"的实践者。按顺序照做即可深入理解项目。
> 环境：Windows + Git Bash，Python 3.12（`.python-version` 锁定）。所有命令在 `deepsearch-agents/` 下执行。

---

## 一、第一次把项目跑起来

### 1. 装依赖

```bash
python -m uv sync --group dev        # 本机无全局 uv，用 python -m uv
```

装好会有 `.venv/`。后续所有 python 命令都用 `.venv/Scripts/python.exe`（或 `python -m uv run ...`）。

### 2. 配 `.env`（真实运行必需）

在 `deepsearch-agents/` 下建 `.env`：

```bash
# 模型（实际是阿里云 DashScope 的 OpenAI 兼容接口）
OPENAI_API_KEY=sk-xxx
LLM_QWEN_MAX=qwen-max

# 网络搜索
TAVILY_API_KEY=tvly-xxx

# MySQL（先起 docker compose）
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3307
MYSQL_USER=deepsearch_ro
MYSQL_PASSWORD=你设的只读密码
MYSQL_DATABASE=deepsearch_db

# 认证（见下"坑"）
API_KEYS=alice:sk-alice-0123456789abcdef
# 本地联调想免密钥时才开，生产别开
# ALLOW_DEV_MODE=1

# 可选覆盖（不配走默认）
# CORS_ORIGINS=http://localhost:5173
# RATE_LIMIT_TASK=10/minute  RATE_LIMIT_UPLOAD=30/minute
# MODEL_RUN_LIMIT=80  MODEL_THREAD_LIMIT=300
# SQL_MAX_LIMIT=1000  MAX_UPLOAD_SIZE_MB=20  MAX_UPLOAD_TOTAL_MB=100
```

### 3. 起 MySQL + 导数据

```bash
cd docker && docker compose up -d mysql
# 首次启动自动执行 01-mysql.sql（建表+数据）+ 02-create-readonly-user.sh（建只读账号）
# 注意：只在数据卷为空时执行；改了 sql 要 docker compose down -v 重建卷
```

### 4. 起后端 + 前端

```bash
# 后端（开两个终端，或后台）
python -m uv run uvicorn app.api.server:app --port 8000 --reload

# 前端
cd frontend && pnpm install && pnpm dev
```

前端默认 `http://localhost:5173`，它会连后端 `:8000`。

---

## 二、跑测试（不需要任何真实服务，6 秒）

这是改代码后第一件事——测试全绿才能继续。

```bash
python -m uv run pytest tests/ -q
# 期望：129 passed in ~6s
```

### 测试在测什么（分布）

| 文件 | 数 | 领域 |
|------|----|------|
| test_path_safety | 14 | 路径穿越防御 |
| test_sql_guard | 45 | SQL 只读 + 自动 LIMIT + UNION |
| test_api_security | 31 | 上传/会话隔离/回滚 |
| test_auth | 28 | API Key + fail-closed + 非 ASCII |
| test_rate_limit | 7 | slowapi 限流 |
| test_checkpointer | 4 | SQLite 持久化 + middleware 透传 |

**为什么不需要真实服务**：`tests/conftest.py` 在导入 app 前用 `setdefault` 注入占位密钥（`OPENAI_API_KEY`/`TAVILY_API_KEY` 等都是 `test-key-placeholder`），并把 `ALLOW_DEV_MODE=1`、限流放开到 `1000/minute`。测试 mock 掉 `run_deep_agent`，纯打校验逻辑。

### 跑单个用例 / 看输出

```bash
python -m uv run pytest tests/test_sql_guard.py::TestEnforceSelectLimit::test_union_gets_default_limit -v
python -m uv run pytest tests/ -q --tb=short      # 失败时看简短堆栈
python -m uv run pytest tests/ -q -k "union"     # 按名筛选
```

### 改了校验逻辑后必须加测试（项目硬规矩）

`AGENTS.md`：改任何校验逻辑必须同步加测试；API 类测试必须用 autouse fixture 把 `output_dir`/`updated_dir` 指向 `tmp_path`，禁止写真实运行时目录。照 `test_rate_limit.py` 顶部的 `isolate_runtime_dirs` fixture 抄。

---

## 三、看自己改了什么：`git diff df6d52e..HEAD`

> **⚠️ 2026-09-05 更正**：本节原标题为"`git diff main`"，并称"所有改造都在 `production-hardening` 分支，main 与上游对齐"——**该分支不存在**（`git branch -a` 仅 `main`），且增量已在 main 上，故 `git diff main` **输出恒为空**。请使用下面的命令。

```bash
git log --oneline -6
# 66f85e9 评测集扩量到 45 条
# 9506b8a 结构化日志 + trace_id
# c0b5b45 同步 I/O 超时与重试
# c11fd2b 提示词矛盾修复
# f4f1995 CI 处理
# 2a0db27 短时链接令牌

# 本人全部增量（df6d52e 是上游 didilili 的最后一笔，2026-05-18）
git diff --shortstat df6d52e..HEAD      # 53 files changed, 6193 insertions(+), 546 deletions(-)
git diff --stat df6d52e..HEAD           # 逐文件明细
git diff df6d52e..HEAD -- app/tools/db_tools.py   # 看某个文件全量改造
git diff df6d52e..HEAD -- tests/                  # 看测试全量

# 只看本轮 7 笔（已提交未推送）
git diff --shortstat c11fd2b^..HEAD     # 17 files changed, 531 insertions(+), 116 deletions(-)
```

面试前用 `git diff df6d52e..HEAD` 自检：嘴里说的每条改造，代码里都得有对应。

**口径提醒**：不同统计口径数字不同，引用时须指明——全量 53 文件 / +6193；只算生产化改造（自 `70f3162` 起）51 文件 / +6191；排除 `uv.lock` 等锁文件 52 文件 / +6025。旧文档写的"40 文件 / +3649"是中间时点统计，已作废。

---

## 四、跑评测集（P3-2，需真实服务）

```bash
# 自检（不需服务，只验用例结构）
python -m eval.cases
# 期望：共 45 个用例：{'sql': 22, 'routing': 6, 'web': 10, 'multi': 7} + 用例自检通过

# ⚠️ 本命令也已在 CI 中执行（.github/workflows/ci.yml），
#    但它只做结构校验（id 唯一性、字段完整），不调用真实 Agent。

# 跑 SQL 类（最快，不依赖网络）
python -m eval.runner --category sql

# 跑单个
python -m eval.runner --case sql-06-inventory-sum

# 全量 + 写报告
python -m eval.runner --out eval-report.json
```

详细见 `eval/README.md`。

---

## 五、常见坑与纠错（踩过的都列在这）

### 坑 1：直接 import 就崩（tavily 模块级构造）

```bash
python -c "from app.api.server import app"
# 会崩：TavilyClient 需要 TAVILY_API_KEY
```

**原因**：`app/tools/tavily_tool.py` 在**模块级**就 `TavilyClient(api_key=...)`，没 key 直接 import 失败。
**解法**：设环境变量再 import。测试靠 conftest 注入占位 key；命令行跑先 `export TAVILY_API_KEY=x`。

### 坑 2：业务接口返回 503（fail-closed）

没配 `API_KEYS` 且没设 `ALLOW_DEV_MODE=1` → 所有业务接口 503。这是**故意的**（fail-closed，防裸奔）。
**本地联调**：`.env` 里 `ALLOW_DEV_MODE=1`（此时请求归属 `local` 用户）。启动时控制台会打印醒目警告。
**生产**：配 `API_KEYS=用户名:密钥`，删掉 `ALLOW_DEV_MODE`。

### 坑 3：`__main__` 启动路径错（P2-2 已知）

`server.py` 末尾 `uvicorn.run("api.server:app", ...)` 的导入路径是错的（应是 `app.api.server:app`）。**别用 `python app/api/server.py` 启动**，用 `uvicorn app.api.server:app --reload`。

### 坑 4：运行时目录被提交 / 被测试污染

`app/data/checkpoints.sqlite3`、`app/output/`、`app/updated/` 是运行时产物，已 gitignore，别提交。测试用 autouse fixture 指向 `tmp_path`（见 `test_rate_limit.py`），不要让测试写真实 `output/`。

### 坑 5：改了 SQL/路径校验但测试没跟上

`AGENTS.md` 硬规矩：改校验逻辑必须同步加测试。否则 129 绿不代表你的新逻辑对。照现有测试类结构加。

### 坑 6：Windows 换行警告

git 提交时 `LF will be replaced by CRLF` 警告——正常，Windows 行尾归一化，不影响功能。

---

## 六、定位问题的通用方法

| 现象 | 怎么查 |
|------|--------|
| 接口 503 | 看 `.env` 有没有 `API_KEYS` 或 `ALLOW_DEV_MODE=1`；启动日志会打印认证状态 |
| 接口 401 | 密钥不对 / 非 ASCII；`auth.py::authenticate_api_key` 走 `compare_digest`，非 ASCII 直接 401 |
| 接口 429 | 触发限流；调高 `RATE_LIMIT_TASK` 或等一分钟；`server.limiter.reset()` 清状态（测试用） |
| 任务卡住不返回 | 看后端控制台 `[Monitor:...]` 输出，确认卡在哪一步；可能是模型调用超上限（`MODEL_RUN_LIMIT` 默认 80） |
| SQL 被拒 | `db_tools.py::assert_readonly_sql` + `enforce_select_limit`；看返回的错误文本，多半是带了写关键字或解析失败 |
| 文件下载 400 | path 不在会话目录内（路径收容校验挡了 `../`） |
| 前端连不上后端 | 看 `frontend/.env` 的 `VITE_API_URL` 和后端 CORS；CORS_ORIGINS 要含前端地址 |

### 日志在哪

现状只有 `print`（P2-1 已知缺陷）。后端控制台直接看 `[Monitor:事件类型] ...`、`[Server] ...`、`[MainAgent] ...`。没有结构化日志/指标/告警——这是已记录的待办。

### 看数据库里有什么

```bash
docker exec -it <mysql容器> mysql -uroot -p deepsearch_db -e "SHOW TABLES; SELECT drug_id,generic_name,therapeutic_area FROM drugs LIMIT 5;"
```

---

## 七、深入项目的推荐顺序

1. 读 `AGENTS.md`（工作区指引，改代码前必读）；
2. `git diff --stat df6d52e..HEAD` 看全貌（**不要用 `git diff main`**，增量已在 main 上，该命令恒为空）；
3. 读 `面试/01-项目结构与核心逻辑解析.md`（结构）→ `02-安全防护机制详解.md`（安全）→ `03-现存缺陷与改进路线.md`（缺陷）→ `04-面试官问题清单与参考回答.md`（问答）；
4. 跑测试 `pytest tests/ -q` 确认绿；
5. 配 `.env` 起服务，前端发一个真实任务，看后端 `[Monitor]` 日志走一遍主智能体→子智能体→工具→结果的链路；
6. 跑评测集 `python -m eval.runner --category sql`，对比"模型实际答的"和"ground truth"；
7. 读 `docs/PRODUCTION_NOTES.md` + `docs/TASK_QUEUE_DESIGN.md` + `docs/EVENT_REPLAY_DESIGN.md` 看下一步；
8. 挑一个小缺陷（如 P2-1 把某个文件的 print 换成 logging）练手，加测试，提交。

按这个顺序走一遍，项目从"看过文档"变成"能改能调"。
