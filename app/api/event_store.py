"""
监控事件持久化存储（P0-3 事件回放，SQLite 实现）

设计目标（详见 docs/design/EVENT_REPLAY_DESIGN.md）：
1. monitor 发出的每条事件先落库再推送，事件"发完即丢"改为可回放；
2. 每条事件携带全局自增 seq，前端据此检测丢件（seq 跳号）并请求差量补发；
3. WebSocket 重连握手携带 last_seq，服务端只补发 last_seq 之后的事件。

为什么用 SQLite 而不是设计稿里的 Redis Stream：
- 当前仍是单进程部署，引入 Redis 只为存事件不划算（第一性原理：先解决
  "断线丢事件"这个真实缺陷，不提前引入用不上的基础设施）；
- 本模块对外只暴露 append / read_after 两个语义，与 Redis Stream 的
  XADD / XREAD 一一对应，P0-2 任务出进程接入 Redis 时只需换实现类，
  monitor 与 WS 端点的调用代码不变；
- seq 采用 SQLite AUTOINCREMENT 全局自增，单流内严格递增，语义上等价于
  Redis Stream 的消息 ID。

已知边界（与设计稿一致的取舍）：
- 每个 task_key 只保留最近 EVENT_MAX_PER_STREAM 条（等价 XADD MAXLEN），
  超出部分写入时裁剪；更早的 last_seq 补发时拿到的差量可能不完整，
  由前端 seq 跳号检测兜底；
- 单连接串行写入（aiosqlite 单连接天然串行），没有多副本并发写场景。
"""

import asyncio
import datetime
import json
import os
from pathlib import Path
from typing import Any, Optional

import aiosqlite

# 当前文件位于 app/api/event_store.py，parents[1] 即 app 目录
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 表结构：seq 是全局自增主键（事件序号），task_key 即复合键 "{user_id}-{thread_id}"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key   TEXT    NOT NULL,
    event_type TEXT    NOT NULL,
    message    TEXT    NOT NULL,
    data       TEXT    NOT NULL,
    ts         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_monitor_events_task_key ON monitor_events(task_key, seq);
"""


class SqliteEventStore:
    """
    基于 SQLite 的监控事件存储

    事件写入与差量读取的接口语义与 Redis Stream 对齐，便于后续替换后端。
    连接采用惰性初始化：首次写入/读取时才建库建连接并复用，避免模块导入
    （如测试收集）就产生文件和连接。
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        max_per_stream: Optional[int] = None,
        replay_limit: Optional[int] = None,
    ) -> None:
        # 路径与容量参数在实例化时解析，测试可通过构造参数显式指定 tmp_path。
        # env 空值容错用 or 链（同 main_agent.CHECKPOINT_DB）：
        # .env 里 EVENT_DB= 留空时 os.getenv 返回 "" 而非 default，
        # Path("") 会让 aiosqlite 报 "unable to open database file"
        self.db_path = (
            db_path
            or os.getenv("EVENT_DB")
            or str(_PROJECT_ROOT / "data" / "events.sqlite3")
        )
        self.max_per_stream = max_per_stream or int(
            os.getenv("EVENT_MAX_PER_STREAM") or "1000"
        )
        # 首次连接（无 last_seq）时的默认补发条数：页面刷新可恢复最近一轮执行轨迹
        self.replay_limit = replay_limit or int(
            os.getenv("EVENT_REPLAY_LIMIT") or "100"
        )
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> aiosqlite.Connection:
        """返回复用的数据库连接，首次调用时建目录、建库、建表"""
        if self._conn is not None:
            return self._conn
        async with self._lock:
            if self._conn is not None:
                return self._conn
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(self.db_path)
            # WAL 模式：服务重启/中断时库文件不易损坏，与 checkpointer 同策略
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.executescript(_SCHEMA)
            await conn.commit()
            self._conn = conn
            return conn

    async def append(
        self,
        task_key: str,
        event_type: str,
        message: str,
        data: Optional[dict[str, Any]] = None,
    ) -> int:
        """
        追加一条事件并返回其序号 seq

        写入加锁串行化：保证 seq 分配顺序与调用顺序一致。前端依赖
        "同一条流内 seq 连续递增" 来检测丢件，乱序分配会破坏该前提。
        """
        conn = await self._ensure()
        async with self._lock:
            cursor = await conn.execute(
                "INSERT INTO monitor_events(task_key, event_type, message, data, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    task_key,
                    event_type,
                    message,
                    json.dumps(data or {}, ensure_ascii=False),
                    datetime.datetime.now().isoformat(),
                ),
            )
            seq = int(cursor.lastrowid)

            # 限长裁剪（等价 XADD MAXLEN ~ N）：防长会话把事件库撑到无限大。
            # 与 INSERT 同一事务单次提交：少一次 WAL fsync，且要么都生效要么都不生效
            await conn.execute(
                "DELETE FROM monitor_events WHERE task_key = ? AND seq NOT IN "
                "(SELECT seq FROM monitor_events WHERE task_key = ? "
                "ORDER BY seq DESC LIMIT ?)",
                (task_key, task_key, self.max_per_stream),
            )
            await conn.commit()
            return seq

    async def read_after(
        self,
        task_key: str,
        last_seq: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """
        读取某个 task_key 的事件差量，返回可直接经 WS 下发的 payload 列表

        :param last_seq: 调用方已收到的最大 seq。
            - 提供 last_seq：返回 seq 严格大于它的全部事件（重连差量补发，
              上限 max_per_stream——更早的已被裁剪，是天然边界）；
            - 不提供：返回最近 limit 条（首次连接/页面刷新的兜底恢复）。
        :param limit: 单次读取上限；缺省时按上述两种场景分别取
            replay_limit / max_per_stream。

        审查修复 R-2：显式 last_seq 原先误用 replay_limit（默认 100）做上限，
        断线累积更多事件时一轮补发补不完，需要前端多轮"跳号→再补发"，
        会烧穿补发预算。差量补发语义上等价 XREAD（读尽积压），此处与
        XADD MAXLEN 的裁剪边界对齐。
        """
        conn = await self._ensure()

        if last_seq is None:
            max_rows = limit if limit is not None else self.replay_limit
            rows = await conn.execute_fetchall(
                "SELECT seq, event_type, message, data, ts FROM monitor_events "
                "WHERE task_key = ? ORDER BY seq DESC LIMIT ?",
                (task_key, max_rows),
            )
            rows.reverse()  # 最近 N 条取完后翻回正序下发
        else:
            max_rows = limit if limit is not None else self.max_per_stream
            rows = await conn.execute_fetchall(
                "SELECT seq, event_type, message, data, ts FROM monitor_events "
                "WHERE task_key = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
                (task_key, last_seq, max_rows),
            )

        return [self._mark_replay(self._to_payload(row)) for row in rows]

    @staticmethod
    def _to_payload(row: tuple) -> dict[str, Any]:
        """把存储行还原成与 monitor 实时推送一致的事件结构（含 seq）"""
        seq, event_type, message, data, ts = row
        return {
            "type": "monitor_event",
            "event": event_type,
            "message": message,
            "data": json.loads(data),
            "timestamp": ts,
            "seq": seq,
        }

    @staticmethod
    def _mark_replay(payload: dict[str, Any]) -> dict[str, Any]:
        """给补发事件打回放标记：前端只对实时流做 seq 跳号检测，
        否则事件流被裁剪后补发批次自身跳号会触发无限补发循环"""
        payload["replay"] = True
        return payload

    async def close(self) -> None:
        """关闭连接（进程退出或测试清理时调用）"""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


# 全局单例：monitor（写入）与 server WS 端点（回放读取）共用同一存储
event_store = SqliteEventStore()
