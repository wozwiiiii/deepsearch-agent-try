# deepsearch-agents 工作状态报告

> **导出时间**：2026-08-17
> **分支**：`production-hardening`（基于 `main` 分支）
> **最新提交**：`d2bc74e` 生产化第二批：API Key 认证 + 多租户隔离 + SQLite 持久化 checkpointer

---

## 一、整体进度概览

| 阶段 | 状态 | 提交 |
|------|------|------|
| 第一批：安全加固（路径/SQL/会话隔离） | ✅ 已提交 | `d0f6eed` → `70f3162` |
| 第二批：认证 + 多租户 + SQLite 持久化 | ✅ 已提交 | `d2bc74e` |
| 第三批：限流 + fail-closed + 模型调用上限 + 审查修复 | ✅ 已提交 | 15 modified + 4 new（CI / 只读用户脚本 / 限流测试 / 状态报告） |
| 面试文档（4 份） | ⚠️ 第二批版本（**未更新第三批内容**） | — |
| CI 流水线 | ✅ 已创建文件，**未提交** | `.github/workflows/ci.yml` |

---

## 二、第三批改动清单（未提交）

### 2.1 改动统计

- **修改文件**：15 个（+540 行 / -38 行）
- **新增文件**：4 个（`tests/test_rate_limit.py`、`.github/workflows/ci.yml`、`docker/mysql/02-create-readonly-user.sh`、`docs/WORK_STATUS.md`）
- **全量测试**：**129 个用例全部通过**（6.94s）

### 2.2 按功能模块分组

#### A. 接口限流（slowapi）— `app/api/server.py` (+127 行)

| 变更 | 说明 |
|------|------|
| `Limiter` + `key_func` | 按密钥 SHA-256 截断 16 位计配额，无密钥回退 IP |
| `@limiter.limit` 装饰器 | `/api/task` 限 `RATE_LIMIT_TASK`（默认 10/min），`/api/upload` 限 `RATE_LIMIT_UPLOAD`（默认 30/min） |
| 429 异常处理 | `RateLimitExceeded` → JSON `{"detail":"..."}`, 标准剩余额度响应头 |
| lifespan 警告 | 启动时打印认证配置状态（已配置密钥 / 开发模式 / 503 报错） |

#### B. Fail-closed 开发模式 — `app/api/auth.py` (+36/-12 行)

| 变更 | 说明 |
|------|------|
| `is_dev_mode_enabled()` | 仅 `ALLOW_DEV_MODE=1` 时启用开发模式 |
| Fail-closed 逻辑 | 未配置 `API_KEYS` + 未开启 dev → 业务接口返回 **503** |
| 非 ASCII 密钥防御 | 直接返回 401 而非 `compare_digest` TypeError → 500 |

#### C. 模型调用硬上限 — `app/agent/main_agent.py` (+25 行)

| 变更 | 说明 |
|------|------|
| `ModelCallLimitMiddleware` | `run_limit=80`（单次任务）/ `thread_limit=300`（单会话累计） |
| 环境变量可配 | `MODEL_RUN_LIMIT` / `MODEL_THREAD_LIMIT` |
| `exit_behavior="error"` | 超限抛异常 → `run_deep_agent` 捕获 → monitor 告知前端 |

#### D. SQL 自动 LIMIT — `app/tools/db_tools.py` (+43 行)

| 变更 | 说明 |
|------|------|
| `enforce_select_limit()` | sqlglot 解析 SELECT 语句，无 LIMIT 时自动追加 `LIMIT 1000` |
| 环境变量可配 | `SQL_MAX_LIMIT`（默认 1000） |
| 解析失败拒绝 | sqlglot ParseError → `SQLSafetyError`，不执行 |

#### E. 代码审查修复

| 编号 | 文件 | 修复 |
|------|------|------|
| R-1 | `server.py` | 新增 `MAX_UPLOAD_TOTAL_SIZE` + `MAX_UPLOAD_FILES`，写盘前拒绝超量 |
| R-2 | `server.py` | `_rollback_saved()`：任一文件失败清理已写入的全部文件 |
| R-3 | `auth.py` | 非 ASCII 密钥返回 401（审查修复） |
| R-4 | `monitor.py` | 新增 `report_error()` 公开接口；测试文件运行时目录隔离到 `tmp_path` |

#### F. 测试

| 文件 | 用例数 | 覆盖内容 |
|------|--------|----------|
| `tests/test_rate_limit.py`（**新建**） | 7 | 限流键构造、任务/上传 429、独立配额、认证先于限流 |
| `tests/test_auth.py`（新增类） | +5 | fail-closed 503、dev mode 放行/拒绝非"1"值、endpoint/WS |
| `tests/test_sql_guard.py`（新增类） | +7 | 自动 LIMIT、已有 LIMIT 保留、JOIN 语义、SHOW 放行、解析错误、完整链路 |
| `tests/test_checkpointer.py`（新增类） | +4 | middleware 透传验证（run_limit/thread_limit 分别断言） |
| `tests/test_api_security.py`（新增） | +4 | 文件数超限、总量超限+回滚、非法扩展名+回滚 |
| `tests/conftest.py` | 修改 | `ALLOW_DEV_MODE=1` + 高默认限流上限（1000/min） |

#### G. 基础设施

| 文件 | 说明 |
|------|------|
| `.github/workflows/ci.yml`（**新建**，59 行） | GitHub Actions CI：Python 3.12 + uv pytest / Node 22 + pnpm tsc -b |
| `docker/mysql/02-create-readonly-user.sh`（**新建**，22 行） | 创建 `deepsearch_ro@%` 只读用户，`GRANT SELECT` |
| `docker/docker-compose.yaml` | 挂载 initdb 脚本 02 + `MYSQL_READONLY_PASSWORD` |
| `pyproject.toml` | 新增 `slowapi>=0.1.10`、`sqlglot>=30.17.0` |
| `.env.example` | 新增 8 个环境变量（限流/成本/dev mode/upload 总量） |

---

## 三、测试分布

| 测试文件 | 用例数 | 覆盖领域 |
|----------|--------|----------|
| `tests/test_path_safety.py` | 14 | 路径穿越防御 |
| `tests/test_sql_guard.py` | 45 | SQL 只读 + 自动 LIMIT + UNION |
| `tests/test_api_security.py` | 31 | 上传安全 + 会话隔离 + 回滚 |
| `tests/test_auth.py` | 28 | API Key 认证 + fail-closed + 非 ASCII |
| `tests/test_rate_limit.py` | 7 | slowapi 限流 |
| `tests/test_checkpointer.py` | 4 | SQLite 持久化 + middleware 透传 |
| **合计** | **129** | — |

> 注：实测分布（`pytest --co` 统计）：path 14 / sql 45 / api 31 / auth 28 / rate_limit 7 / checkpointer 4，合计 129。早期版本误记 sql_guard 为 34（连带总数误记 118）；第三批后 sql_guard 增至 45（含 UNION 等集合操作补 LIMIT 用例），以实测为准。

---

## 四、面试文档状态

4 份文档保存于 `D:\AI_Program\hello-agents-my-build\面试\`，已全部同步至第三批后状态（129 测试）：

| 文档 | 版本 | 状态 |
|------|------|----------|
| `01-项目结构与核心逻辑解析.md` | 第二版（数字已同步） | 测试数 129、提交清单含三批；正文聚焦前两批结构/安全，第三批见 PRODUCTION_NOTES |
| `02-安全防护机制详解.md` | 第二版（数字已同步） | 测试数 129、分项含 UNION 补 LIMIT；正文聚焦前两批漏洞 |
| `03-现存缺陷与改进路线.md` | 第三版 | 含第三批内容；P1-6 SQL 自动 LIMIT（含 UNION 残留）已标完成；测试数 129 |
| `04-面试官问题清单与参考回答.md` | 第三版 | 含限流/fail-closed/调用上限问答；测试数 129 |

---

## 五、尚未完成的待办项

| 优先级 | 任务 | 状态 |
|--------|------|------|
| ✅ **P0** | Git 提交第三批改动（15 modified + 4 new） | 已完成（62f13ab） |
| ✅ **P0** | 更新 `docs/PRODUCTION_NOTES.md` 测试计数（118→125→129、sql_guard 34→41→45）及审查修复节 | 已完成 |
| ✅ **P1** | 更新 `面试/` 4 份文档（数字同步至 129、03 的 P1-6 标完成） | 已完成 |
| ✅ **P1** | 修复审查发现：UNION 等集合操作漏过 SQL 自动 LIMIT（enforce_select_limit 扩展 SetOperation + 4 用例） | 已完成（本 commit） |
| **P2** | 上线路线图中的企业级差距（任务队列、可观测性、评测体系） | 长期 |

---

## 六、企业级差距路线图（长期）

| # | 差距 | 阻塞性 | 备注 |
|---|------|--------|------|
| 1 | 任务队列与并发治理 | 🔴 上线必须 | `asyncio.create_task` → Celery/ARQ/Temporal |
| 2 | 可观测性（结构化日志 + OTel + Prometheus） | 🟡 重要 | 替换 `print`，接入 LangSmith/LangFuse |
| 3 | 评测体系（固定评测集 + LLM-as-judge + CI 回归） | 🟡 重要 | Agent 项目区别于 demo 的核心证据 |
| 4 | Token 级预算熔断 | 🟢 改进 | 当前只有调用次数上限，无 token 累计 |
| 5 | 水平扩容 checkpoint（SQLite → Postgres） | 🟢 改进 | 多副本部署时换 `langgraph-checkpoint-postgres` |

---

*本报告由 ZCode 自动生成，基于 `production-hardening` 分支工作区状态。*
