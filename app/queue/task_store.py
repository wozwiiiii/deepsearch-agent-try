"""
任务状态外置存储（P0-2 阶段 1）：Postgres task 表 DAO

设计要点（详见 docs/design/TASK_QUEUE_DESIGN.md 阶段 1）：
1. 任务状态从进程内 active_tasks dict 外置到 Postgres tasks 表，
   取消/状态查询/重拾都不再依赖"提交任务的进程还活着"；
2. 复合键 {user_id}-{thread_id} 仍是主键（与 active_tasks、WS 路由、
   LangGraph thread_id 同一形式），多租户隔离的根不动；
3. job_id 约定（关键）：一律 f"task:{task_key}:{submit_count}"——ARQ 用
   _job_id 做 in-flight 去重，若固定 task_key 会导致旧 job 吞掉新提交
   （任务永远 pending），因此每次提交 submit_count+1 并重建 job_id；
4. 状态机：pending → running → done | failed | cancelled。
   mark_finished 带 WHERE status IN ('pending','running') 守卫，
   终态一旦落表不可被覆盖（防迟到的旧 worker 覆盖新提交的结果）。

错误处理风格沿用现仓库：DAO 层抛 psycopg 原生异常，server 层（task_service）
负责转 HTTPException。
"""

import asyncio
import os
from typing import Any, Optional

from app.utils.logging_setup import get_logger

logger = get_logger(__name__)

# 默认 DSN 与 docker/docker-compose.yaml 的 postgres 服务保持一致（映射 5433）。
# 注意用 or 而非 getenv 第二参数（同 main_agent.CHECKPOINT_DB）：
# .env 里 POSTGRES_DSN= 留空时 os.getenv 返回 ""，or 链回退默认值
POSTGRES_DSN = (
    os.getenv("POSTGRES_DSN")
    or "postgresql://deepsearch:deepsearch@localhost:5433/deepsearch_tasks"
)

# 非终态集合：取消/替换/重拾/落终态的 WHERE 守卫都用它
_UNFINISHED_STATUSES = ("pending", "running")

_TASK_COLUMNS = (
    "task_key",
    "user_id",
    "thread_id",
    "query",
    "status",
    "submit_count",
    "job_id",
    "worker_id",
    "result",
    "error",
    "started_at",
    "finished_at",
    "created_at",
    "updated_at",
)

# DDL 幂等（IF NOT EXISTS），init_schema 可在 API 启动与 worker 启动各调一次。
# CREATE TABLE 与 CREATE INDEX 分开执行：psycopg 无参数时走 simple query
# 协议虽可多语句，但拆开对错误定位更友好
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    task_key VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(15) NOT NULL,
    thread_id VARCHAR(48) NOT NULL,
    query TEXT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    submit_count INTEGER NOT NULL DEFAULT 0,
    job_id VARCHAR(96),
    worker_id VARCHAR(64),
    result TEXT,
    error TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)"


class PostgresTaskStore:
    """
    基于 Postgres 的任务状态存储

    连接池惰性初始化：首次调用时才建 AsyncConnectionPool 并复用，避免模块
    导入（如测试收集、inline 模式的纯 API 进程）就建立数据库连接。
    psycopg_pool 同样延迟导入：inline 模式即使没装新依赖也完全不受影响。
    """

    def __init__(self, dsn: Optional[str] = None) -> None:
        # dsn 参数供测试显式指定（tmp 库）；缺省读 POSTGRES_DSN 环境变量
        self.dsn = dsn or POSTGRES_DSN
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_pool(self) -> Any:
        """返回复用的 psycopg AsyncConnectionPool，首次调用时创建"""
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is not None:
                return self._pool
            try:
                from psycopg_pool import AsyncConnectionPool
            except ImportError as e:  # pragma: no cover - 依赖缺失的引导性报错
                raise RuntimeError(
                    "队列模式需要 psycopg 连接池支持："
                    "请安装 psycopg[binary,pool]（见 requirements.txt）"
                ) from e
            # open=False 显式创建再打开：避免在构造函数里隐式启动后台任务，
            # await open() 保证 min_size 个连接就绪后再服务
            pool = AsyncConnectionPool(self.dsn, min_size=1, max_size=5, open=False)
            await pool.open()
            self._pool = pool
            logger.info(f"[TaskStore] Postgres 连接池已创建: {self._safe_dsn()}")
            return pool

    def _safe_dsn(self) -> str:
        """日志用脱敏 DSN：只保留 host:port/dbname，不回显密码"""
        try:
            tail = self.dsn.rsplit("@", 1)[-1]
            return tail.split("/", 1)[0] + "/" + tail.split("/", 1)[1].split("?")[0]
        except Exception:
            return "<dsn>"

    @staticmethod
    def _row_to_dict(row: tuple) -> dict[str, Any]:
        """把 SELECT * 行按固定列序还原成 dict（get_task/list_unfinished 共用）"""
        return dict(zip(_TASK_COLUMNS, row))

    async def init_schema(self) -> None:
        """幂等建表建索引：API 进程与 worker 进程启动时各调一次都安全"""
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            await conn.execute(_CREATE_TABLE_SQL)
            await conn.execute(_CREATE_INDEX_SQL)
        logger.info("[TaskStore] tasks 表结构已就绪")

    async def create_or_replace_task(
        self,
        task_key: str,
        user_id: str,
        thread_id: str,
        query: str,
    ) -> str:
        """
        提交任务：同 key 旧行若非终态先置 cancelled（对齐现有"同会话替换"
        语义），再 upsert 新行 status=pending、submit_count+1。

        :returns: job_id（f"task:{task_key}:{submit_count}"，供 ARQ 入队去重）

        关键实现：ON CONFLICT DO UPDATE 里 submit_count=tasks.submit_count+1，
        job_id 用同一事务内的旧值 +1 重建——保证"重提交必然产生新 job_id"，
        否则 ARQ 会按 _job_id 去重吞掉新提交（任务永远 pending）。
        整个 UPDATE + INSERT 在同一事务内，原子生效。
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            # 第一步：同 key 的旧非终态行先置 cancelled（终态行不动，
            # 由下面 ON CONFLICT 的 DO UPDATE 直接覆盖为新一轮 pending）
            await conn.execute(
                "UPDATE tasks SET status = 'cancelled', finished_at = now(), "
                "updated_at = now() "
                "WHERE task_key = %s AND status IN ('pending', 'running')",
                (task_key,),
            )
            # 第二步：upsert 新一轮任务并按旧 submit_count+1 重建 job_id
            cursor = await conn.execute(
                """
                INSERT INTO tasks
                    (task_key, user_id, thread_id, query, status, submit_count, job_id)
                VALUES
                    (%s, %s, %s, %s, 'pending', 1, 'task:' || %s || ':1')
                ON CONFLICT (task_key) DO UPDATE SET
                    query = EXCLUDED.query,
                    status = 'pending',
                    submit_count = tasks.submit_count + 1,
                    job_id = 'task:' || tasks.task_key || ':'
                             || (tasks.submit_count + 1)::text,
                    worker_id = NULL,
                    result = NULL,
                    error = NULL,
                    started_at = NULL,
                    finished_at = NULL,
                    updated_at = now()
                RETURNING submit_count, job_id
                """,
                (task_key, user_id, thread_id, query, task_key),
            )
            row = await cursor.fetchone()
            if row is None:  # pragma: no cover - RETURNING 理论必有行
                raise RuntimeError(f"任务 upsert 未返回行: {task_key}")
            submit_count, job_id = row
            logger.info(
                f"[TaskStore] 任务已提交: {task_key} "
                f"submit_count={submit_count} job_id={job_id}"
            )
            return str(job_id)

    async def mark_running(self, task_key: str, worker_id: str) -> bool:
        """
        pending → running（worker 领取任务时调用）

        :returns: True 表示领取成功；False 说明任务已不是 pending
            （入队后被取消/替换），worker 应跳过执行
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE tasks SET status = 'running', worker_id = %s, "
                "started_at = now(), updated_at = now() "
                "WHERE task_key = %s AND status = 'pending'",
                (worker_id, task_key),
            )
            return cursor.rowcount == 1

    async def mark_finished(
        self,
        task_key: str,
        status: str,
        result: Optional[str] = None,
        error: Optional[str] = None,
        worker_id: Optional[str] = None,
    ) -> bool:
        """
        落终态（done | failed | cancelled）

        WHERE 守卫（两层）：
        1. status IN ('pending','running')：终态不可被覆盖；
        2. worker_id 代际守卫（QA 回归 P0-1）：worker 在 mark_running 时写入
           的 worker_id 即任务代际身份——同会话替换会把行重置为新一轮
           （worker_id=NULL，新 worker 领取后写入新 worker_id），迟到的旧
           worker 落终态时 worker_id 不匹配即被拒绝，防止把"新提交的
           pending 行"误标为旧 run 的结果（真库复现场景：A running →
           重提交 B → 旧 worker 收尾 done 污染 B）。

        :param worker_id: 调用方（worker）在 mark_running 时使用的身份；
            传入即启用代际守卫，None 保持旧行为（仅终态守卫，测试用）
        :returns: True 表示成功落终态；False 说明被守卫拦下
        """
        if status not in ("done", "failed", "cancelled"):
            raise ValueError(f"非法终态: {status}")
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            if worker_id is not None:
                cursor = await conn.execute(
                    "UPDATE tasks SET status = %s, result = %s, error = %s, "
                    "finished_at = now(), updated_at = now() "
                    "WHERE task_key = %s AND status IN ('pending', 'running') "
                    "AND worker_id = %s",
                    (status, result, error, task_key, worker_id),
                )
            else:
                cursor = await conn.execute(
                    "UPDATE tasks SET status = %s, result = %s, error = %s, "
                    "finished_at = now(), updated_at = now() "
                    "WHERE task_key = %s AND status IN ('pending', 'running')",
                    (status, result, error, task_key),
                )
            return cursor.rowcount == 1

    async def reset_running(self, task_key: str) -> bool:
        """
        崩溃恢复：running → pending（清 worker_id/started_at）

        供 worker 启动重拾调用：recovered 的 running 行若保持 running，
        run_task 的"非 pending 即跳过"会让重入队永远空转、任务卡死 running
        （QA 回归 P1-1）。重置后由任意 worker 重新领取执行。
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE tasks SET status = 'pending', worker_id = NULL, "
                "started_at = NULL, updated_at = now() "
                "WHERE task_key = %s AND status = 'running'",
                (task_key,),
            )
            return cursor.rowcount == 1

    async def cancel_task(self, task_key: str) -> Optional[str]:
        """
        用户取消：非终态 → cancelled

        :returns: 被取消前的状态（'pending' 或 'running'），供 server 层区分
            返回 cancelled 还是 cancelling；任务不存在或已是终态返回 None
        """
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            # FOR UPDATE 锁行后再判断再更新：与 worker 的 mark_finished
            # 并发时保证"读到 running → 写 cancelled"不丢
            cursor = await conn.execute(
                "SELECT status FROM tasks WHERE task_key = %s FOR UPDATE",
                (task_key,),
            )
            row = await cursor.fetchone()
            if row is None or row[0] not in _UNFINISHED_STATUSES:
                return None
            await conn.execute(
                "UPDATE tasks SET status = 'cancelled', finished_at = now(), "
                "updated_at = now() WHERE task_key = %s",
                (task_key,),
            )
            return str(row[0])

    async def get_task(self, task_key: str) -> Optional[dict[str, Any]]:
        """按复合键查任务行；不存在返回 None"""
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM tasks WHERE task_key = %s", (task_key,)
            )
            row = await cursor.fetchone()
            return self._row_to_dict(row) if row else None

    async def list_unfinished(self) -> list[dict[str, Any]]:
        """列出 pending/running 任务（worker 启动时重拾用）"""
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM tasks WHERE status IN ('pending', 'running') "
                "ORDER BY updated_at ASC"
            )
            rows = await cursor.fetchall()
            return [self._row_to_dict(row) for row in rows]

    async def close(self) -> None:
        """关闭连接池（worker 退出或测试清理时调用）"""
        if self._pool is not None:
            pool = self._pool
            self._pool = None
            await pool.close()
            logger.info("[TaskStore] Postgres 连接池已关闭")


# 全局单例：task_service（API 进程）与 worker（执行进程）共用同一访问入口
task_store = PostgresTaskStore()
