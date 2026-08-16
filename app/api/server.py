"""
FastAPI 接口层与项目闭环入口

负责承接前端的任务提交、任务取消、文件上传/下载、输出文件列表查询和
WebSocket 长连接。HTTP 接口只做轻量调度，真正的 DeepAgents 执行放到后台
任务中；执行进度、工具调用和最终结果由 monitor 按 thread_id 推送给前端。

生产化改造说明（详见 docs/PRODUCTION_NOTES.md）：
1. thread_id 统一过白名单校验，杜绝通过会话 ID 拼接出越界目录；
2. 上传接口增加扩展名白名单、单文件大小上限和文件名清洗，异步分块落盘；
3. 文件列表/下载不再接受客户端传入的绝对路径，改为按 thread_id 在服务端
   拼接会话目录，天然消除跨会话越权浏览与下载；
4. CORS 收敛为可配置白名单；错误统一返回正确的 HTTP 状态码。
"""

import asyncio
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

import aiofiles
import uvicorn
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.agent.main_agent import run_deep_agent
from app.api.monitor import manager

# ---------------------------------------------------------------------------
# 安全校验常量与工具函数
# ---------------------------------------------------------------------------

# thread_id 会拼进文件系统路径和 LangGraph 配置，只放行安全字符，
# 服务端生成的 uuid4 天然满足该格式
_THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 上传文件扩展名白名单：与 read_file_content 工具支持的解析格式保持一致
ALLOWED_UPLOAD_EXTENSIONS = {".md", ".txt", ".pdf", ".docx", ".xlsx", ".xls", ".csv"}

# 单文件大小上限（字节），默认 20MB，可用 MAX_UPLOAD_SIZE_MB 调整
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "20"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_SIZE_MB * 1024 * 1024

# 任务文本最大长度，防止异常超长输入直接打满模型上下文
MAX_QUERY_LENGTH = 10_000

# CORS 允许来源，逗号分隔配置；默认只放行本地 Vite 开发服务器
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if origin.strip()
]


def validate_thread_id(thread_id: str) -> str:
    """
    校验 thread_id 格式，非法值直接抛 400

    :param thread_id: 客户端传入或服务端生成的会话 ID
    :return: 通过校验的 thread_id
    """
    if not thread_id or not _THREAD_ID_PATTERN.match(thread_id):
        raise HTTPException(
            status_code=400,
            detail="非法的 thread_id：仅允许 1-64 位字母、数字、下划线或连字符",
        )
    return thread_id


def sanitize_filename(filename: str) -> str:
    """
    清洗上传文件名，只保留 basename 并拒绝可疑字符

    :param filename: 客户端上传的原始文件名
    :return: 清洗后的纯文件名
    """
    # Path(...).name 会剥掉所有目录部分，阻断 ../../evil.txt 这类穿越写法
    safe_name = Path(filename.replace("\\", "/")).name
    if not safe_name or safe_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="非法的文件名")
    if "/" in safe_name or "\\" in safe_name or "\x00" in safe_name:
        raise HTTPException(status_code=400, detail="非法的文件名")
    return safe_name


def session_output_dir(thread_id: str) -> Path:
    """返回指定会话的输出目录，调用方需先通过 validate_thread_id 校验"""
    return output_dir / f"session_{thread_id}"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    服务生命周期入口。

    启动时绑定当前事件循环到 WebSocket 管理器，确保后台 Agent 任务可以把
    monitor 事件投递回 FastAPI 所在的 loop。
    """
    loop = asyncio.get_running_loop()
    manager.set_loop(loop)
    print(f"[Server] WebSocket Manager bound to loop: {id(loop)}")
    yield


# 当前文件位于 app/api/server.py，运行时目录统一收敛到 app 目录
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent

app = FastAPI(title="DeepAgents API", lifespan=lifespan)

# 保存 thread_id -> 后台 Agent 任务，用于同一会话任务替换和主动取消
active_tasks: dict[str, asyncio.Task] = {}

# output 保存每个会话最终工作区，前端只允许从这里浏览和下载生成文件
output_dir = project_root / "output"
output_dir.mkdir(exist_ok=True)

# updated 暂存用户上传文件，run_deep_agent 启动时会复制到对应 output/session_xxx
updated_dir = project_root / "updated"
updated_dir.mkdir(exist_ok=True)

# 教学项目前后端分别本地启动，跨域收敛为可配置白名单而非全放开；
# 部署到公网时务必通过 CORS_ORIGINS 指定真实前端域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TaskRequest(BaseModel):
    """前端启动任务时提交的请求体。"""

    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    thread_id: str = None


def _forget_task(thread_id: str, task: asyncio.Task) -> None:
    """
    清理已结束任务的登记关系。

    done_callback 触发时，active_tasks 中可能已经被新任务替换；只有仍是同一个
    task 时才删除，避免误清理同 thread_id 下刚启动的新任务。
    """
    if active_tasks.get(thread_id) is task:
        active_tasks.pop(thread_id, None)


@app.get("/health")
async def health():
    """健康检查端点，供负载均衡和容器探针使用"""
    return {"status": "ok"}


@app.post("/api/task")
async def run_task(request: TaskRequest):
    """
    启动一次 DeepAgents 后台任务。

    HTTP 请求只负责创建后台协程并立即返回，后续执行轨迹、子智能体调用和最终
    答案都会由 monitor 通过 `/ws/{thread_id}` 推送给同一会话的前端。
    """
    thread_id = validate_thread_id(request.thread_id or str(uuid.uuid4()))

    # 同一个 thread_id 只保留一个活跃任务，新任务会先取消旧任务，避免并发写同一会话目录
    old_task = active_tasks.get(thread_id)
    if old_task and not old_task.done():
        old_task.cancel()

    # create_task 把长耗时 Agent 执行交给事件循环，接口本身不用等待最终结果
    task = asyncio.create_task(run_deep_agent(request.query, thread_id))
    active_tasks[thread_id] = task
    task.add_done_callback(lambda finished_task: _forget_task(thread_id, finished_task))

    return {"status": "started", "thread_id": thread_id}


@app.post("/api/task/{thread_id}/cancel")
async def cancel_task(thread_id: str):
    """
    取消指定 thread_id 对应的后台 Agent 任务。

    注意：取消会向 asyncio.Task 注入 CancelledError。若底层第三方工具正在执行不可中断
    的同步阻塞调用，任务可能需要等该调用返回后才会真正结束。
    """
    validate_thread_id(thread_id)
    task = active_tasks.get(thread_id)
    if not task or task.done():
        active_tasks.pop(thread_id, None)
        raise HTTPException(status_code=404, detail="任务不存在或已结束")

    # 先发出取消信号，再短暂等待协程响应；若底层阻塞中，则返回 cancelling 给前端继续展示状态
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        _forget_task(thread_id, task)
        return {"status": "cancelled", "thread_id": thread_id}
    except asyncio.TimeoutError:
        return {"status": "cancelling", "thread_id": thread_id}
    except Exception as e:
        _forget_task(thread_id, task)
        return {"status": "cancelled", "thread_id": thread_id, "message": str(e)}

    _forget_task(thread_id, task)
    return {"status": "cancelled", "thread_id": thread_id}


@app.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(...), thread_id: str = Form(...)):
    """
    文件上传接口 (File Upload)。

    目标：
    1. 接收用户上传的一个或多个文件，校验扩展名白名单和大小上限。
    2. 清洗文件名后保存到 `updated/session_{thread_id}` 目录。
    3. 供 Agent 在后续任务中读取和分析。

    Args:
        files (List[UploadFile]): 文件对象列表。
        thread_id (str): 关联的任务会话 ID。
    """
    validate_thread_id(thread_id)

    # 上传文件先按会话隔离保存，避免不同任务读取到彼此的附件
    target_dir = updated_dir / f"session_{thread_id}"
    target_dir.mkdir(parents=True, exist_ok=True)

    saved_files = []
    for file in files:
        safe_name = sanitize_filename(file.filename or "")

        ext = Path(safe_name).suffix.lower()
        if ext not in ALLOWED_UPLOAD_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型 '{ext}'，仅允许: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}",
            )

        file_path = target_dir / safe_name
        written = 0
        try:
            # 异步分块写入：既不阻塞事件循环，也能在超限时立即中止并清理半成品
            async with aiofiles.open(file_path, "wb") as buffer:
                while chunk := await file.read(1024 * 1024):
                    written += len(chunk)
                    if written > MAX_UPLOAD_SIZE:
                        raise HTTPException(
                            status_code=413,
                            detail=f"文件 '{safe_name}' 超过大小上限 {MAX_UPLOAD_SIZE_MB}MB",
                        )
                    await buffer.write(chunk)
        except HTTPException:
            # 超限或写入失败时删除不完整的文件，避免残留半成品被 Agent 读取
            file_path.unlink(missing_ok=True)
            raise
        except Exception as e:
            file_path.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"保存文件失败: {e}")

        saved_files.append(safe_name)

    return {"status": "uploaded", "files": saved_files}


@app.get("/api/files")
async def list_files(thread_id: str):
    """
    文件列表查询接口 (File Explorer)。

    只接受 thread_id，由服务端拼接对应的会话输出目录；客户端无法指定任意
    路径，因此不存在跨会话枚举其他用户产物的可能。返回的 path 为会话目录
    内的相对路径，供下载接口使用。

    Args:
        thread_id (str): 会话 ID。
    """
    validate_thread_id(thread_id)
    session_dir = session_output_dir(thread_id)

    if not session_dir.exists():
        raise HTTPException(status_code=404, detail="会话目录不存在")

    files = []
    try:
        # 递归返回文件元数据，前端据此渲染文件列表并发起下载请求
        for file_path in session_dir.rglob("*"):
            if file_path.is_file():
                stat = file_path.stat()
                files.append(
                    {
                        "name": file_path.name,
                        "type": "file",
                        "path": file_path.relative_to(session_dir).as_posix(),
                        "thread_id": thread_id,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"遍历文件失败: {e}")

    # 最新生成的文件排在前面，方便用户优先看到本次任务产物
    files.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return {"files": files}


@app.get("/api/download")
async def download_file(thread_id: str, path: str):
    """
    文件下载接口 (File Download)。

    下载范围由服务端根据 thread_id 决定（output/session_{thread_id}），path
    参数只允许是会话目录内的相对路径；resolve 后再做一次收容校验，双保险
    阻断 ../ 形式的路径穿越。

    Args:
        thread_id (str): 会话 ID。
        path (str): 会话目录内的相对路径（来自 /api/files 返回值）。
    """
    validate_thread_id(thread_id)
    session_dir = session_output_dir(thread_id).resolve()

    abs_path = (session_dir / path).resolve()
    if not abs_path.is_relative_to(session_dir):
        raise HTTPException(status_code=400, detail="拒绝访问: 只能下载当前会话目录内的文件")

    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    # FileResponse 会以流式响应返回文件内容，并让浏览器使用原文件名下载
    return FileResponse(abs_path, filename=abs_path.name)


@app.websocket("/ws/{thread_id}")
async def websocket_endpoint(websocket: WebSocket, thread_id: str):
    """
    WebSocket 实时通讯核心接口 (Real-time Communication)。

    连接建立后，ConnectionManager 会用 thread_id 保存 WebSocket。monitor 后续
    发送事件时只需要按 thread_id 查找连接，就能把进度推给对应页面。循环中的
    receive_text 用于接收前端心跳，避免连接空闲断开。
    """
    validate_thread_id(thread_id)

    # 连接建立后立即按 thread_id 注册，monitor 后续才能把事件定向推给当前页面
    await manager.connect(websocket, thread_id)

    try:
        while True:
            # 前端通常发送 ping 心跳；服务端回复 pong，顺便维持连接活跃
            data = await websocket.receive_text()
            await websocket.send_json(
                {"type": "pong", "message": f"服务端已收到: {data}"}
            )

    except WebSocketDisconnect:
        # 只移除当前 WebSocket 实例，避免旧连接断开时误删同 thread_id 的新连接
        manager.disconnect(websocket, thread_id)
        print(f"[WebSocket] 客户端已断开: {thread_id}")

    except Exception as e:
        print(f"[WebSocket] 连接异常: {e}")
        manager.disconnect(websocket, thread_id)


if __name__ == "__main__":
    uvicorn.run("api.server:app", host="0.0.0.0", port=8000, reload=True)
