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
4. CORS 收敛为可配置白名单；错误统一返回正确的 HTTP 状态码；
5. 全部业务接口经 X-API-Key 认证，会话目录/上传目录/任务取消/WebSocket
   推送均按租户 user_id 隔离（app/api/auth.py）。
"""

import asyncio
import hashlib
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

import aiofiles
import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse

from app.agent.main_agent import run_deep_agent
from app.api.auth import (
    Principal,
    authenticate_api_key,
    is_dev_mode_enabled,
    issue_link_token,
    require_principal,
    resolve_link_token,
)
from app.api.event_store import event_store
from app.api.monitor import manager
from app.utils.logging_setup import get_logger, setup_logging

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 安全校验常量与工具函数
# ---------------------------------------------------------------------------

# thread_id 会拼进文件系统路径和 LangGraph 配置，只放行安全字符。
# 上限 48 位：与用户前缀拼成 "{user_id}-{thread_id}" 后不超过 64 位
_THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,48}$")

# 上传文件扩展名白名单：与 read_file_content 工具支持的解析格式保持一致
ALLOWED_UPLOAD_EXTENSIONS = {".md", ".txt", ".pdf", ".docx", ".xlsx", ".xls", ".csv"}

# 单文件大小上限（字节），默认 20MB，可用 MAX_UPLOAD_SIZE_MB 调整
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "20"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_SIZE_MB * 1024 * 1024

# 单请求上传总大小上限（字节），默认 100MB：单文件限制可被"一次传很多文件"
# 绕过，必须同时限制请求级总量，防止磁盘耗尽（代码审查修复 M-1）
MAX_UPLOAD_TOTAL_MB = int(os.getenv("MAX_UPLOAD_TOTAL_MB", "100"))
MAX_UPLOAD_TOTAL_SIZE = MAX_UPLOAD_TOTAL_MB * 1024 * 1024

# 单请求文件数上限：海量小文件同样能绕过字节级限制
MAX_UPLOAD_FILES = 20

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

# ---------------------------------------------------------------------------
# 限流（第三批改造）：按"密钥身份"限流，防止合法密钥无限烧 LLM/Tavily 费用
# ---------------------------------------------------------------------------

# 任务与上传的限流阈值（limits 库语法：次数/时间窗），可用环境变量覆盖
RATE_LIMIT_TASK = os.getenv("RATE_LIMIT_TASK", "10/minute")
RATE_LIMIT_UPLOAD = os.getenv("RATE_LIMIT_UPLOAD", "30/minute")
# 令牌签发限流：令牌本身短时有效，但签发接口会分配随机值并写登记表，
# 无限流的话可被刷出内存增长（每条 256 位 + 元数据），按密钥限频兜底
RATE_LIMIT_TOKEN = os.getenv("RATE_LIMIT_TOKEN", "30/minute")


def _rate_limit_key(request: Request) -> str:
    """
    限流键：携带 API Key 的请求按密钥哈希计，开发模式回退按客户端 IP 计

    密钥做 SHA-256 截断后再入键，避免明文密钥留在限流器的内存状态和日志里。
    注意：反向代理后面所有用户共享 IP，生产部署应配置可信的 X-Forwarded-For
    解析（PROXY_COUNT）或确保所有客户端都携带密钥。
    """
    api_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if api_key:
        digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
        return f"key:{digest}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=_rate_limit_key)


def validate_thread_id(thread_id: str) -> str:
    """
    校验 thread_id 格式，非法值直接抛 400

    :param thread_id: 客户端传入或服务端生成的会话 ID
    :return: 通过校验的 thread_id
    """
    if not thread_id or not _THREAD_ID_PATTERN.match(thread_id):
        raise HTTPException(
            status_code=400,
            detail="非法的 thread_id：仅允许 1-48 位字母、数字、下划线或连字符",
        )
    return thread_id


def user_scope_dir(base_dir: Path, user_id: str, thread_id: str) -> Path:
    """
    返回某个租户某次会话在 base_dir 下的工作目录

    目录结构 base/user_{user_id}/session_{thread_id} 保证不同租户的产物
    与上传文件在文件系统层面天然隔离。
    """
    return base_dir / f"user_{user_id}" / f"session_{thread_id}"


def composite_task_key(user_id: str, thread_id: str) -> str:
    """
    生成任务级复合键：active_tasks、WebSocket 路由和 LangGraph thread_id
    共用同一形式 "{user_id}-{thread_id}"，确保 A 用户无法取消/接收 B 用户的任务

    user_id 限定为纯小写字母数字（见 auth.py），复合键因此无歧义。
    """
    return f"{user_id}-{thread_id}"


async def require_principal_for_link(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    token: str | None = None,
    api_key: str | None = None,
) -> Principal:
    """
    浏览器直链（下载、WebSocket）的认证依赖，按安全优先级依次尝试：

    1. X-API-Key 请求头（fetch/axios 可带，密钥不进 URL）；
    2. 短时链接令牌（P1-2，60 秒有效，替代查询参数里的长期密钥）；
    3. api_key 查询参数——兼容期保留的旧入口，密钥会进访问日志/
       浏览器历史/Referer，前端已不再发送，计划随 P0-2 移除。

    注意开发模式（未配置 API_KEYS）：三个凭据都为空时经
    authenticate_api_key 走 ALLOW_DEV_MODE 分支，行为与之前一致。
    """
    if x_api_key:
        return authenticate_api_key(x_api_key)
    if token:
        return resolve_link_token(token)
    return authenticate_api_key(api_key)


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


def _rollback_saved(paths: List[Path]) -> None:
    """
    多文件上传中途失败时，清理本次请求已写入的全部文件。

    不回滚会留下"部分上传"状态：前 N-1 个文件已落盘但请求整体失败，
    后续任务可能把这批不完整附件当作有效输入（代码审查修复 L-2）。
    """
    for path in paths:
        path.unlink(missing_ok=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    服务生命周期入口。

    启动时绑定当前事件循环到 WebSocket 管理器，确保后台 Agent 任务可以把
    monitor 事件投递回 FastAPI 所在的 loop；同时检查认证配置并给出醒目提示。
    """
    # 先让 root logger 输出统一 JSON（含 trace_id/user_id/thread_id），
    # 再开始打启动日志，保证后续所有运行日志都走结构化格式
    setup_logging()

    loop = asyncio.get_running_loop()
    manager.set_loop(loop)
    logger.info(f"[Server] WebSocket Manager bound to loop: {id(loop)}")

    # 启动时显式暴露认证配置状态，避免"忘配密钥裸奔"或"拒绝服务"排查困难
    if os.getenv("API_KEYS", "").strip():
        logger.info("[Server] 认证已启用：API_KEYS 已配置")
    elif is_dev_mode_enabled():
        logger.warning(
            "[Server][警告] 开发模式运行中：未配置 API_KEYS，所有请求归属 local 用户。\n"
            "[Server][警告] 该模式仅限本地联调，禁止暴露到公网。\n"
            "[Server][警告] 部署前请在 .env 配置 API_KEYS=用户名:密钥 并移除 ALLOW_DEV_MODE。"
        )
    else:
        logger.error(
            "[Server][错误] 未配置 API_KEYS 且未设置 ALLOW_DEV_MODE=1，"
            "所有业务接口将返回 503。请配置 API_KEYS 后重启。"
        )
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

# slowapi 限流器注册：装饰器方案要求把 limiter 挂到 app.state 并注册超限处理器
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    """超过限流阈值时返回 429，并在响应头中携带剩余额度信息（slowapi 提供）"""
    return JSONResponse(
        status_code=429,
        content={"detail": "请求过于频繁，请稍后再试"},
        headers=getattr(exc, "headers", None) or {},
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


@app.post("/api/token")
@limiter.limit(lambda: RATE_LIMIT_TOKEN)
async def create_link_token(
    request: Request, principal: Principal = Depends(require_principal)
):
    """
    签发短时链接令牌（每密钥限 RATE_LIMIT_TOKEN 次/分钟，P1-2）。

    客户端先经 X-API-Key 请求头认证换取 60 秒令牌，再把它拼进
    WebSocket 握手 URL——长期密钥不再出现在任何 URL 中，访问日志/
    浏览器历史里最多留下一个一分钟内失效的随机值。
    """
    token, expires_in = issue_link_token(principal.user_id)
    return {"token": token, "expires_in": expires_in}


@app.post("/api/task")
@limiter.limit(lambda: RATE_LIMIT_TASK)
async def run_task(
    request: Request, body: TaskRequest, principal: Principal = Depends(require_principal)
):
    """
    启动一次 DeepAgents 后台任务（每密钥限 RATE_LIMIT_TASK 次/分钟）。

    HTTP 请求只负责创建后台协程并立即返回，后续执行轨迹、子智能体调用和最终
    答案都会由 monitor 通过 `/ws/{thread_id}` 推送给同一会话的前端。
    """
    thread_id = validate_thread_id(body.thread_id or str(uuid.uuid4()))
    task_key = composite_task_key(principal.user_id, thread_id)

    # 同一用户同一 thread_id 只保留一个活跃任务；复合键保证跨租户互不影响
    old_task = active_tasks.get(task_key)
    if old_task and not old_task.done():
        old_task.cancel()

    # create_task 把长耗时 Agent 执行交给事件循环，接口本身不用等待最终结果
    task = asyncio.create_task(
        run_deep_agent(body.query, thread_id, principal.user_id)
    )
    active_tasks[task_key] = task
    task.add_done_callback(lambda finished_task: _forget_task(task_key, finished_task))

    return {"status": "started", "thread_id": thread_id}


@app.post("/api/task/{thread_id}/cancel")
async def cancel_task(thread_id: str, principal: Principal = Depends(require_principal)):
    """
    取消指定 thread_id 对应的后台 Agent 任务。

    注意：取消会向 asyncio.Task 注入 CancelledError。若底层第三方工具正在执行不可中断
    的同步阻塞调用，任务可能需要等该调用返回后才会真正结束。
    """
    validate_thread_id(thread_id)
    task_key = composite_task_key(principal.user_id, thread_id)
    task = active_tasks.get(task_key)
    if not task or task.done():
        active_tasks.pop(task_key, None)
        raise HTTPException(status_code=404, detail="任务不存在或已结束")

    # 先发出取消信号，再短暂等待协程响应；若底层阻塞中，则返回 cancelling 给前端继续展示状态
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        _forget_task(task_key, task)
        return {"status": "cancelled", "thread_id": thread_id}
    except asyncio.TimeoutError:
        return {"status": "cancelling", "thread_id": thread_id}
    except Exception as e:
        _forget_task(task_key, task)
        return {"status": "cancelled", "thread_id": thread_id, "message": str(e)}

    _forget_task(task_key, task)
    return {"status": "cancelled", "thread_id": thread_id}


@app.post("/api/upload")
@limiter.limit(lambda: RATE_LIMIT_UPLOAD)
async def upload_files(
    request: Request,
    files: List[UploadFile] = File(...),
    thread_id: str = Form(...),
    principal: Principal = Depends(require_principal),
):
    """
    文件上传接口 (File Upload)（每密钥限 RATE_LIMIT_UPLOAD 次/分钟）。

    目标：
    1. 接收用户上传的一个或多个文件，校验扩展名白名单和大小上限。
    2. 清洗文件名后保存到当前租户的 `updated/user_{uid}/session_{thread_id}` 目录。
    3. 供 Agent 在后续任务中读取和分析。

    Args:
        files (List[UploadFile]): 文件对象列表。
        thread_id (str): 关联的任务会话 ID。
    """
    validate_thread_id(thread_id)

    # 文件数上限在写盘之前拒绝：海量小文件可绕过字节级限制（审查修复 M-1）
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"单次最多上传 {MAX_UPLOAD_FILES} 个文件，当前 {len(files)} 个",
        )

    # 上传文件按「租户 + 会话」两级隔离保存，避免不同用户读取到彼此的附件
    target_dir = user_scope_dir(updated_dir, principal.user_id, thread_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    saved_files = []
    saved_paths: List[Path] = []
    total_written = 0
    for file in files:
        safe_name = sanitize_filename(file.filename or "")

        ext = Path(safe_name).suffix.lower()
        if ext not in ALLOWED_UPLOAD_EXTENSIONS:
            # 任何一个文件校验失败，本次请求整体失败：回滚已写入的文件
            _rollback_saved(saved_paths)
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
                    if total_written + written > MAX_UPLOAD_TOTAL_SIZE:
                        raise HTTPException(
                            status_code=413,
                            detail=f"本次上传总大小超过上限 {MAX_UPLOAD_TOTAL_MB}MB",
                        )
                    await buffer.write(chunk)
        except HTTPException:
            # 超限或写入失败时删除不完整的文件，并回滚本次已保存的文件
            file_path.unlink(missing_ok=True)
            _rollback_saved(saved_paths)
            raise
        except Exception as e:
            file_path.unlink(missing_ok=True)
            _rollback_saved(saved_paths)
            raise HTTPException(status_code=500, detail=f"保存文件失败: {e}")

        total_written += written
        saved_paths.append(file_path)
        saved_files.append(safe_name)

    return {"status": "uploaded", "files": saved_files}


@app.get("/api/files")
async def list_files(thread_id: str, principal: Principal = Depends(require_principal)):
    """
    文件列表查询接口 (File Explorer)。

    只接受 thread_id，由服务端拼接当前租户的会话输出目录；客户端无法指定任意
    路径，也无法看到其他租户的任何产物。返回的 path 为会话目录内的相对路径，
    供下载接口使用。

    Args:
        thread_id (str): 会话 ID。
    """
    validate_thread_id(thread_id)
    session_dir = user_scope_dir(output_dir, principal.user_id, thread_id)

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
async def download_file(
    thread_id: str,
    path: str,
    principal: Principal = Depends(require_principal_for_link),
):
    """
    文件下载接口 (File Download)。

    鉴权按安全优先级：X-API-Key 头 > 短时令牌（token 参数）> api_key
    查询参数（兼容期旧入口）；前端已改为 fetch + 请求头 + blob 下载，
    正常路径下任何形式的密钥都不会出现在 URL 中。

    下载范围由服务端根据「当前租户 + thread_id」决定，path 参数只允许是
    会话目录内的相对路径；resolve 后再做一次收容校验，双保险阻断 ../ 形式
    的路径穿越。其他租户即使传入相同 thread_id 也只会定位到自己的目录。

    Args:
        thread_id (str): 会话 ID。
        path (str): 会话目录内的相对路径（来自 /api/files 返回值）。
    """
    validate_thread_id(thread_id)
    session_dir = user_scope_dir(output_dir, principal.user_id, thread_id).resolve()

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

    浏览器 WebSocket 无法自定义请求头，鉴权经短时令牌或查询参数传递（见下方
    P1-2 说明）。连接建立后按「租户+thread_id」复合键注册，monitor 事件按
    同一复合键定向推送，保证不同租户即使使用相同 thread_id 也不会收到彼此
    的执行事件。

    P0-3 事件回放：握手可携带 last_seq 查询参数（前端已收到的最大事件序号），
    服务端先从事件库补发 last_seq 之后的差量，再注册实时推送。断线期间的
    事件（含最终答案）不再丢失；不带 last_seq 的首次连接补发最近
    EVENT_REPLAY_LIMIT 条，页面刷新也能恢复上一轮执行轨迹。

    P1-2 鉴权：浏览器无法为 WS 设置请求头，推荐先经 POST /api/token
    换取 60 秒短时令牌再连接；api_key 查询参数为兼容期旧入口（前端已
    不再发送）；开发模式（未配置 API_KEYS）无凭据直接放行。
    """
    try:
        link_token = websocket.query_params.get("token")
        if link_token:
            principal = resolve_link_token(link_token)
        else:
            principal = authenticate_api_key(
                websocket.query_params.get("api_key")
            )
    except HTTPException:
        # 未 accept 直接 close，Starlette 会以 403 拒绝握手
        await websocket.close(code=1008)
        return

    validate_thread_id(thread_id)

    # last_seq 必须是非负整数：非法值直接拒绝握手，避免歧义补发
    last_seq_raw = websocket.query_params.get("last_seq")
    last_seq = None
    if last_seq_raw is not None:
        if not re.fullmatch(r"[0-9]{1,18}", last_seq_raw):
            await websocket.close(code=1008)
            return
        last_seq = int(last_seq_raw)

    routing_key = composite_task_key(principal.user_id, thread_id)

    await websocket.accept()

    # 先补发历史差量、再注册实时推送：注册提前会导致补发期间的新事件
    # 与历史事件交错下发，前端按 seq 检测丢件时会误判为乱序
    try:
        replayed = await event_store.read_after(routing_key, last_seq)
    except Exception as e:
        # 事件库故障不阻断连接：降级为无回放的纯实时模式
        logger.warning(f"[WebSocket] 事件回放读取失败（降级为纯实时模式）: {e}")
        replayed = []
    for event_payload in replayed:
        await websocket.send_json(event_payload)

    # 补发完成后再按复合键注册，monitor 后续才能把事件定向推给当前页面
    manager.register(websocket, routing_key)

    try:
        while True:
            # 前端通常发送 ping 心跳；服务端回复 pong，顺便维持连接活跃
            data = await websocket.receive_text()
            await websocket.send_json(
                {"type": "pong", "message": f"服务端已收到: {data}"}
            )

    except WebSocketDisconnect:
        # 只移除当前 WebSocket 实例，避免旧连接断开时误删同 thread_id 的新连接
        manager.disconnect(websocket, routing_key)
        logger.info(f"[WebSocket] 客户端已断开: {routing_key}")

    except Exception as e:
        logger.error(f"[WebSocket] 连接异常: {e}", exc_info=True)
        manager.disconnect(websocket, routing_key)


if __name__ == "__main__":
    uvicorn.run("api.server:app", host="0.0.0.0", port=8000, reload=True)
