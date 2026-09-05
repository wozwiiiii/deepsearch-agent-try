"""
Agent 执行过程监控模块

负责把工具调用、子智能体调用、任务结果和会话目录等事件统一包装后推送给前端
在 Web 服务中优先通过 WebSocket 定向推送；在脚本调试场景中保留控制台输出

P0-3 事件回放改造：每条事件先持久化到 event_store 拿到全局自增 seq，
再推送给前端。断线期间的事件不丢失，前端重连时凭 last_seq 差量补发；
seq 跳号即说明有丢件，前端可主动请求补发。持久化失败只降级为
"该事件不可回放"，不影响实时推送。
"""

import asyncio
import builtins
import datetime
from typing import Any, Optional

from fastapi import WebSocket

from app.api.context import get_thread_context
from app.api.event_store import event_store
from app.utils.logging_setup import get_logger

logger = get_logger(__name__)


class ToolMonitor:
    """
    工具和助手调用的统一监控入口

    业务工具只需要导入全局 monitor，并调用 report_tool/report_assistant 等方法
    具体是通过 WebSocket 推送，还是输出到脚本运行时，由本类内部统一处理
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ToolMonitor, cls).__new__(cls)
            cls._instance.websocket_manager = None
            # 最近一次「持久化+推送」的调度句柄（Task 或 concurrent Future）。
            # 生产代码不使用；测试用它同步等待异步落库完成，避免轮询
            cls._instance._last_emit_handle = None
        return cls._instance

    def set_websocket_manager(self, manager: "ConnectionManager") -> None:
        """绑定 FastAPI WebSocket 连接管理器"""
        self.websocket_manager = manager

    def _emit(
        self,
        event_type: str,
        message: str,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        构造统一监控事件，持久化后推送到当前 thread_id 对应的前端连接

        :param event_type: 事件类型，例如 tool_start、assistant_call
        :param message: 面向前端展示的事件说明
        :param data: 附加结构化数据
        """
        payload = {
            "type": "monitor_event",
            "event": event_type,
            "message": message,
            "data": data or {},
            "timestamp": datetime.datetime.now().isoformat(),
        }

        # 事件落库 + WS 推送都需要事件循环：优先用 WS 管理器绑定的主循环，
        # 兜底用当前协程所在循环（脚本调试场景）。两者都没有时跳过，
        # 只保留下方控制台输出。
        manager_loop = self.websocket_manager.loop if self.websocket_manager else None
        if manager_loop is None:
            try:
                manager_loop = asyncio.get_running_loop()
            except RuntimeError:
                manager_loop = None

        try:
            thread_id = get_thread_context()
        except Exception:
            thread_id = None

        if manager_loop and thread_id:
            self._schedule_persist_and_send(payload, thread_id, manager_loop)

        # DeepAgents 脚本调试时，如果运行时暴露了 stream_writer，也同步写入流式输出
        if hasattr(builtins, "runtime") and hasattr(builtins.runtime, "stream_writer"):
            try:
                builtins.runtime.stream_writer(payload)
            except Exception:
                pass

        # 控制台保底输出，便于无前端场景下观察执行过程
        logger.info(f"[Monitor:{event_type}] {message}")

    def _schedule_persist_and_send(
        self,
        payload: dict[str, Any],
        thread_id: str,
        target_loop: asyncio.AbstractEventLoop,
    ) -> None:
        """
        把「持久化 + 推送」协程投递到目标事件循环

        工具可能在 LangChain 的线程池里同步执行，不在 FastAPI 主循环上，
        因此沿用线程安全投递；已在同一循环时直接 create_task。
        投递顺序即执行顺序，同一任务内事件不会被乱序调度。
        """
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        coroutine = self._persist_and_send(payload, thread_id)
        if current_loop is not None and current_loop is target_loop:
            self._last_emit_handle = current_loop.create_task(coroutine)
        else:
            self._last_emit_handle = asyncio.run_coroutine_threadsafe(
                coroutine, target_loop
            )

    async def _persist_and_send(
        self, payload: dict[str, Any], thread_id: str
    ) -> None:
        """
        事件先落库取得 seq，再推送给前端（P0-3 回放的核心链路）

        顺序约束：seq 必须在推送前写入 payload，前端才能用 seq 检测丢件。
        持久化失败时事件仍实时推送（降级为"该事件不可回放"），不让存储
        故障阻塞执行过程反馈。
        """
        try:
            seq = await event_store.append(
                thread_id, payload["event"], payload["message"], payload["data"]
            )
            payload["seq"] = seq
        except Exception as e:
            logger.warning(f"[Monitor] 事件持久化失败（该事件将不可回放）: {e}")

        if self.websocket_manager:
            try:
                await self.websocket_manager.send_to_thread(payload, thread_id)
            except Exception as e:
                logger.error(f"[Monitor] WebSocket send failed: {e}", exc_info=True)

    def report_tool(
        self,
        tool_name: str,
        args: Optional[dict[str, Any]] = None,
    ) -> None:
        """报告开始执行某个工具"""
        self._emit(
            "tool_start",
            f"开始执行工具: {tool_name}",
            {"tool_name": tool_name, "args": args},
        )

    def report_assistant(
        self,
        assistant_name: str,
        args: Optional[dict[str, Any]] = None,
    ) -> None:
        """报告正在调用某个子智能体"""
        self._emit(
            "assistant_call",
            f"正在调用助手: {assistant_name}",
            {"assistant_name": assistant_name, "args": args},
        )

    def report_task_result(self, result: str) -> None:
        """报告任务最终结果"""
        self._emit("task_result", "任务执行完成", {"result": result})

    def report_task_cancelled(self) -> None:
        """报告任务已被用户取消"""
        self._emit("task_cancelled", "任务已取消")

    def report_error(self, message: str) -> None:
        """报告任务执行错误（公开接口，供 API/Agent 层上报异常事件）"""
        self._emit("error", message)

    def report_session_dir(self, path: str) -> None:
        """报告当前任务工作目录"""
        self._emit("session_created", f"工作目录已创建: {path}", {"path": path})


monitor = ToolMonitor()


class ConnectionManager:
    """
    WebSocket 连接管理器

    active_connections 使用 thread_id 作为 key，保证监控事件只推送给对应任务的前端连接
    """

    def __init__(self) -> None:
        self.active_connections: dict[str, WebSocket] = {}
        # WebSocket 发送必须回到创建连接的事件循环，因此启动时需要显式绑定 loop
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定 FastAPI 主事件循环，并同步注册到 monitor"""
        self.loop = loop
        monitor.set_websocket_manager(self)
        logger.info(f"[Monitor] ConnectionManager manually bound to loop: {id(self.loop)}")

    def register(self, websocket: WebSocket, thread_id: str) -> None:
        """
        按 thread_id 注册连接，此后 monitor 事件定向推送给该连接

        注册与 accept 分离：WS 端点需要先补发历史差量事件、再注册实时
        推送，否则补发期间新事件会与历史事件交错下发，破坏 seq 顺序。
        """
        self.active_connections[thread_id] = websocket
        logger.info(f"Client connected: {thread_id}")

    def disconnect(self, websocket: WebSocket, thread_id: str) -> None:
        """移除已经断开的 WebSocket 连接"""
        if self.active_connections.get(thread_id) is websocket:
            del self.active_connections[thread_id]
            logger.info(f"Client disconnected: {thread_id}")
        else:
            logger.info(f"Stale websocket disconnected, current connection kept: {thread_id}")

    async def send_personal_message(self, message: str, websocket: WebSocket) -> None:
        """向指定 WebSocket 发送纯文本消息"""
        await websocket.send_text(message)

    async def send_to_thread(self, message: dict[str, Any], thread_id: str) -> None:
        """向指定 thread_id 对应的前端连接发送 JSON 消息"""
        if thread_id in self.active_connections:
            websocket = self.active_connections[thread_id]
            await websocket.send_json(message)


manager = ConnectionManager()
