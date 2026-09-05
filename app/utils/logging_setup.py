"""
统一结构化日志配置模块

用 Python 标准库 logging + contextvars + uuid 实现单行 JSON 结构化日志，
在 API 层 → agent 层 → tools 层贯穿 trace_id / user_id / thread_id，
使故障可按 trace_id 秒级定位。零第三方依赖。

约定：
- 各模块用 `logger = get_logger(__name__)` 取日志器；
- 服务启动时调用一次 setup_logging()，root logger 统一输出 JSON；
- trace_id / user_id / thread_id 从 app.api.context 的 ContextVar 读取，缺失时用 "-" 占位。
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from app.api.context import (
    get_thread_context,
    get_trace_context,
    get_user_context,
)

# ContextVar 未设置时的占位符：表示"无该维度标识"
_MISSING = "-"


class TraceContextFilter(logging.Filter):
    """把 contextvars 中的 trace_id / user_id / thread_id 注入到每条日志记录"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_context() or _MISSING
        record.user_id = get_user_context() or _MISSING
        record.thread_id = get_thread_context() or _MISSING
        return True


class JsonFormatter(logging.Formatter):
    """把日志记录格式化为单行 JSON"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": getattr(record, "trace_id", _MISSING),
            "user_id": getattr(record, "user_id", _MISSING),
            "thread_id": getattr(record, "thread_id", _MISSING),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: Optional[str] = None) -> None:
    """
    配置 root logger：StreamHandler + JsonFormatter + TraceContextFilter

    幂等：每次调用先移除 root 上已有的 handler，避免重复输出。
    :param level: 日志级别；缺省时读取环境变量 LOG_LEVEL，再缺省用 INFO
    """
    if level is None:
        level = os.getenv("LOG_LEVEL", "INFO").upper()

    root = logging.getLogger()
    root.setLevel(level)

    # 去掉默认/历史 handler，保证只有一套 JSON 输出
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(TraceContextFilter())
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """返回具名 logger，日志经 root handler 统一输出为 JSON"""
    return logging.getLogger(name)
