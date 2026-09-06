# 代码终审报告（2026-09-06）

> 审查范围：本会话全部代码改动 `d38ad2a..HEAD`——空输出守卫扩展、api_key 查询参数移除、会话级 token 预算、P0-2 阶段 1（任务出进程），共 8 个代码文件 ~1,300 行 + 10 个测试文件 ~1,440 行。
> 审查视角：安全（注入/鉴权/信息泄露）、并发与竞态、资源泄漏、配置健壮性、依赖隔离、文档一致性。
> 前置：本批代码已经 QA 两轮独立回归（Round 1 判返工 → Round 2 真库探针验证闭环），本轮为提交前的终审把关。

## 一、审查结论：通过（1 项当场修复，2 项 P3 建议，6 项 P2 已知遗留）

## 二、逐文件审查结果

| 文件 | 结果 | 要点 |
|------|------|------|
| app/queue/task_store.py（336 行，新增） | ✅ | 全部 SQL 参数化（%s，零拼接注入面）；连接池惰性初始化 + asyncio.Lock 双重检查；DSN 日志脱敏（`_safe_dsn` 不回显密码）；状态机三层守卫完整（终态不可覆盖 / worker_id 代际 / FOR UPDATE 原子取消）；psycopg 事务边界依赖 `pool.connection()` 上下文（退出即 commit），create_or_replace 两步原子成立 |
| app/queue/worker.py（269 行，新增） | ✅ | 竞态路径闭合（get→mark_running 窄竞态有 skipped 兜底）；watch 与 exec 的 FIRST_COMPLETED 各分支穷尽（含同 done 小窗口）；代际守卫覆盖 done/failed/cancelled 三收尾；超时层级正确（660 > 600，Agent 自己的超时收尾先生效）；watch 轮询异常容错不中断；行删除场景（None）靠 exec 600s 兜底不会永久挂 |
| app/api/task_service.py（223 行，新增） | ✅ | inline 分支与原 server.py 逐行等价（QA 对照验证）；生命周期钩子齐全（redis 幂等建表 / arq 池 aclose 兼容旧版） |
| app/api/server.py（+181 行改造） | ✅ | 四处改造点委托干净；status 端点鉴权与归属校验同 cancel 强度（复合键定位，跨租户 404）；WS 轮询桥 send_lock 覆盖轮询与 pong、read_after 异常容错、finally 取消轮询任务 |
| app/agent/main_agent.py（+70 行） | ✅ | 空输出守卫（guard_trigger_reason 统一入口 + last_model_had_tool_calls 区分截断/空输出）；双 saver 分支（redis 延迟导入，inline 零影响）；会话预算接入点在 token 累计处（超限同 RuntimeError 路径） |
| app/agent/token_budget.py（57 行，新增） | ✅ | threading.Lock 防御多线程复用；负值防御；等值不超限语义与 run 级一致；进程边界已在注释如实声明 |
| app/api/auth.py（2 行） | ✅ | 仅 docstring 更新 |
| tests/（10 文件，+1,440 行） | ✅ | conftest 默认 inline 保存量零回归；3 个 patch 点迁移断言零改动；真容器用例无 DSN 自动 skip |

## 三、终审发现与处置

### 当场修复（1 项）
- **EVENT_POLL_SECONDS 空值崩溃**（QA Round 1 的 P2-3）：`.env` 置空时 `float("")` 会在 API 进程导入即崩。已改为仓库惯例的 `or` 链并加注释；实测 `EVENT_POLL_SECONDS=` 空值导入通过。

### P3 建议（不阻塞，记录待后续）
1. `task_service._get_arq_pool` 无并发锁——与 `task_store._ensure_pool` 的双检锁风格不一致；首次并发提交的理论窗口可能创建两个池（后者覆盖前者）。进程生命周期一次性的小泄漏，建议补 asyncio.Lock 对齐风格。
2. `job_id_for` 在 task_service 与 worker.py 双处定义（同实现）——为避免循环 import 的取舍，注释已互相声明一致；若后续改约定需两处同步。

### P2 已知遗留（QA 两轮记录在案，均有明确归属）
- 轮询桥 finally 只捕 CancelledError（send_json 失败会污染 ASGI 日志）；
- send_lock 不覆盖 monitor 直推（改动前已有暴露面，非本次引入）；
- worker 模块导入时全局设置 Windows 事件循环策略（进程级副作用，实测无破坏）；
- recover 对 PG 不可达 fail-fast（建议运维文档补一句）；
- inline 模式轮询桥常开（流量翻倍，前端 seq 去重已验证兼容）；
- 取消路径代际守卫拒绝后的日志文案误导（P3）。

## 四、静态与依赖隔离验证（实测）

- `compileall app/ tests/`：全部可编译；
- **API 进程（inline 默认）真实导入链**：`import app.api.server` 后 sys.modules 无 arq/psycopg/redis/langgraph_checkpoint——**零新依赖验证通过**（评测与开发环境不装新包也完全可跑）；
- worker 导入链：queue_name=deepsearch:tasks、max_tries=1 配置正确；
- pytest 全量：243 passed + 19 skipped（无 PG 环境）/ 262 passed（真容器）。

## 五、文档一致性

TASK_QUEUE_DESIGN（阶段 1 已实现 + 6 处落地差异）、CODE_WIKI（基准/队列层/治理模块）、README/USAGE（队列模式上手）、PRODUCTION_NOTES（worker 启动）、EVENT_REPLAY（阶段 2 前置就绪）均已于提交 `3ec14f7` 同步至代码现状。

## 六、终审签收

代码可发布。本会话累计代码交付：守卫体系（过渡语+空输出，9 救回）、双层 token 预算、文件意图硬约束、api_key 旧入口移除、P0-2 阶段 1 任务出进程——全部经 QA 两轮回归 + 本终审，测试 262 全绿，远端同步至 `3ec14f7`。
