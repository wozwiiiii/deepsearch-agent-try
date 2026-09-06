"""
任务队列模块（P0-2 阶段 1）

- task_store：Postgres task 表 DAO（任务状态外置）
- worker：ARQ worker（Agent 执行出进程），启动命令
  `arq app.queue.worker.WorkerSettings`（详见 docs/status/PRODUCTION_NOTES.md）
"""
