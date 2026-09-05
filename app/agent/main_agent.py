"""
主智能体组装与异步执行模块

负责把模型、主提示词、文件类工具和三个专家子智能体组装成 DeepAgent，
并提供 run_deep_agent 作为后续 API 层调用的统一入口。运行时还会为每个
「租户 + session_id」创建独立工作目录，并把工具调用、子智能体调用和最终
结果推送给前端。

生产化改造（第二批）：
1. 会话目录按租户隔离：output/user_{uid}/session_{sid}、updated 同构；
2. checkpointer 由 InMemorySaver（重启即丢、多副本不共享）替换为
   AsyncSqliteSaver 持久化到本地 SQLite（CHECKPOINT_DB 可配），服务重启后
   同一 thread 可恢复历史状态；
3. LangGraph thread_id 与 WebSocket 路由键统一为 "{user_id}-{session_id}"
   复合键，租户间状态与事件互不可见。
"""

import asyncio
import os
import shutil
from pathlib import Path

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.agent.final_answer_guard import (
    FINAL_ANSWER_GUARD_MAX,
    FINAL_ANSWER_GUARD_PROMPT,
    is_pure_transition_reply,
)
from app.agent.llm import model
from app.agent.prompts import main_agent_content
from app.agent.subagents.database_query_agent import database_query_agent
from app.agent.subagents.knowledge_base_agent import knowledge_base_agent
from app.agent.subagents.network_search_agent import network_search_agent
from app.api.context import (
    reset_session_context,
    set_session_context,
    set_thread_context,
    set_trace_context,
    set_user_context,
)
from app.api.monitor import monitor
from app.utils.logging_setup import get_logger

logger = get_logger(__name__)

# 文件类工具由主智能体直接掌握，负责读取上传附件和生成最终交付文档
from app.tools.markdown_tools import generate_markdown
from app.tools.pdf_tools import convert_md_to_pdf
from app.tools.upload_file_read_tool import read_file_content

# 当前文件位于 app/agent/main_agent.py，parents[1] 即 app 目录
project_root_path = Path(__file__).parents[1].resolve()

# 检查点数据库路径：默认 app/data/checkpoints.sqlite3，可用 CHECKPOINT_DB 覆盖。
# 注意用 or 而非 getenv 第二参数：.env.example 引导用户留空（CHECKPOINT_DB=），
# 而 os.getenv(key, default) 在 key 存在但值为空字符串时返回 ""，
# Path("") 即当前目录，aiosqlite 会报 "unable to open database file"（评测首跑实测踩中）
CHECKPOINT_DB = (
    os.getenv("CHECKPOINT_DB")
    or str(project_root_path / "data" / "checkpoints.sqlite3")
)

# 模型调用次数硬上限（第三批成本治理）：
# - run_limit 限制单次任务内的模型调用总数（主智能体 + 子智能体各自循环都计数），
#   防止提示注入或规划失控导致无限烧 token；
# - thread_limit 限制同一复合 thread_id 的累计调用数（checkpointer 持久化后
#   会话可以跨重启续聊，长会话也需要总量上限）。
# 默认值按"复杂研究任务约 3 个子智能体 × 10-20 轮规划"量级设定，可用环境变量调整
MODEL_RUN_LIMIT = int(os.getenv("MODEL_RUN_LIMIT", "80"))
MODEL_THREAD_LIMIT = int(os.getenv("MODEL_THREAD_LIMIT", "300"))

# 任务级硬超时（P1-4 可靠性）：LLM API 网络半开挂起时任务会永远停在
# "运行中"（前端一直转圈、占用模型调用预算、无法被普通取消打断）。
# 超时取消内部执行流并经 monitor 告知前端；默认 600 秒，可用环境变量调整
TASK_TIMEOUT_SECONDS = float(os.getenv("TASK_TIMEOUT_SECONDS", "600"))

# 单次任务 token 预算（P1-4a 成本熔断）：调用次数上限（MODEL_RUN_LIMIT=80）
# 不约束单次上下文长度——单任务理论消耗可达 80×32K≈256 万 token 无上限。
# 此处按流式返回的 usage_metadata 累计，超预算立即终止任务。
# 默认 150 万：允许 80 次接近满窗的调用（成本上限 ≈ 理论上限的 60%），
# 同时拦截"少数超长调用 + 失控循环"的组合。会话级（跨任务累计）预算
# 需读 checkpoint 历史，暂未实现（见 PRODUCTION_NOTES 边界）。
MODEL_TOKEN_RUN_LIMIT = int(os.getenv("MODEL_TOKEN_RUN_LIMIT", "1500000"))

# 主智能体是调度中心：
# 1. tools 只放最终交付相关的文件工具
# 2. subagents 放网络、数据库、RAGFlow 三类信息获取助手
# 3. checkpointer 依赖复合 thread_id 保存同一会话的执行上下文
#
# 惰性初始化：AsyncSqliteSaver 需要持有长连接，首次执行任务时再创建并复用，
# 避免模块导入（例如测试收集）就建立数据库连接
_main_agent = None
_agent_init_lock = asyncio.Lock()
_checkpoint_saver = None
# 关键：必须同时持有 context manager 对象本身。from_conn_string 是
# @asynccontextmanager（挂起的 async generator），若只保存 __aenter__ 的返回值，
# 局部变量 saver_ctx 在本函数返回后即被 GC，asyncio 的 asyncgen 终结机制会
# aclose 它 → 内层 "async with aiosqlite.connect(...)" 退出 → 正在服务的
# checkpointer 连接被关闭，后续所有 checkpoint 读写报
# "Cannot operate on a closed database"/"Connection closed"（评测首跑实测踩中）
_checkpoint_saver_ctx = None


async def _get_agent():
    """
    返回可复用的主智能体实例，首次调用时完成组装和持久化 checkpointer 初始化
    """
    global _main_agent, _checkpoint_saver, _checkpoint_saver_ctx
    if _main_agent is not None:
        return _main_agent

    async with _agent_init_lock:
        if _main_agent is not None:
            return _main_agent

        db_path = Path(CHECKPOINT_DB)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # from_conn_string 返回异步上下文管理器；这里显式进入并保持到进程结束。
        # 注意 context manager 本身必须存入全局（见 _checkpoint_saver_ctx 注释），
        # 否则被 GC 时会连带关闭正在服务的连接
        saver_ctx = AsyncSqliteSaver.from_conn_string(str(db_path))
        _checkpoint_saver_ctx = saver_ctx
        _checkpoint_saver = await saver_ctx.__aenter__()

        _main_agent = create_deep_agent(
            model=model,
            system_prompt=main_agent_content["system_prompt"],
            tools=[generate_markdown, convert_md_to_pdf, read_file_content],
            checkpointer=_checkpoint_saver,
            subagents=[
                database_query_agent,
                network_search_agent,
                knowledge_base_agent,
            ],
            # 成本硬上限：超限抛异常，由 run_deep_agent 统一捕获并经 monitor 告知前端
            middleware=[
                ModelCallLimitMiddleware(
                    run_limit=MODEL_RUN_LIMIT,
                    thread_limit=MODEL_THREAD_LIMIT,
                    exit_behavior="error",
                )
            ],
        )
        logger.info(
            f"[MainAgent] 已初始化，checkpointer=sqlite:{db_path}，"
            f"模型调用上限 run={MODEL_RUN_LIMIT}/thread={MODEL_THREAD_LIMIT}"
        )
        return _main_agent


async def close_main_agent() -> None:
    """
    显式释放主智能体与 checkpointer 连接（脚本/评测进程退出前调用）

    FastAPI 服务进程常驻，连接随进程生命周期回收即可；但脚本场景
    （eval.runner、诊断探针）跑完就退出，aiosqlite 的 worker 线程是
    非 daemon 线程，不显式关闭会让 threading._shutdown 永久 join，
    进程即使主流程结束也挂住不退（实测 13 分钟不退出的事故组成之一）。
    """
    global _main_agent, _checkpoint_saver, _checkpoint_saver_ctx
    _main_agent = None
    if _checkpoint_saver_ctx is not None:
        ctx = _checkpoint_saver_ctx
        _checkpoint_saver_ctx = None
        _checkpoint_saver = None
        try:
            await ctx.__aexit__(None, None, None)
        except Exception as e:
            logger.warning(f"[MainAgent] 关闭 checkpointer 时异常（忽略）: {e}")


async def _consume_agent_stream(agent_factory, message: str, config: dict) -> None:
    """
    执行主智能体并消费其流式输出（原 run_deep_agent 的内联循环）

    独立成协程是为了让 run_deep_agent 能用 asyncio.wait_for 对整个执行
    （含 Agent 惰性初始化）施加硬超时——异步生成器无法直接套 wait_for。
    :param agent_factory: 返回主智能体实例的协程函数（_get_agent）

    最终回答守卫（P1 早停修复）：ReAct 循环里模型输出"无工具调用的纯文本"
    即宣告回合结束。评测 90 次运行中出现 5 次模型以"我将启动××进行查询。"
    这类纯过渡语收尾的早停，用户只收到一句空话。守卫在每轮回合自然结束后
    检查最终回答：命中"短 + 无数字 + 过渡语"的高精度特征时，向同一会话
    注入纠偏消息强制续跑；守卫最多触发 FINAL_ANSWER_GUARD_MAX 次，续跑后
    仍是过渡语则放行结束（防死循环）。判据实现在
    app/agent/final_answer_guard.py。

    调用上限边界（如实说明）：守卫续跑是第二次独立 astream，而
    ModelCallLimitMiddleware 的 run 级上限（MODEL_RUN_LIMIT=80）以
    UntrackedValue 计数、不跨运行持久化，续跑轮 run 级计数从 0 重新开始
    ——单任务模型调用的实际上限最多放宽到约 2×run_limit；thread 级上限
    （MODEL_THREAD_LIMIT=300，随 checkpoint 持久化）、600s 任务硬超时与
    token 熔断（tokens_used 为局部变量，跨守卫轮累计）仍然有效，作为
    成本兜底。
    """
    # 首次调用会完成 Agent 组装和 SQLite checkpointer 初始化
    # （超时同样覆盖初始化阶段，防数据库异常挂起）
    agent = await agent_factory()

    # 单次任务 token 累计（P1-4a）：按模型消息的 usage_metadata 求和。
    # 部分供应商不回传 usage，此时该机制静默失效（边界见 PRODUCTION_NOTES）
    tokens_used = 0

    # 守卫剩余触发次数：最多 1 次，用尽后过渡语也放行结束（防死循环）
    guard_remaining = FINAL_ANSWER_GUARD_MAX
    # 首轮输入原始任务问题；守卫续跑时替换为纠偏消息（同 thread_id 续会话）
    agent_input: dict = {"messages": [{"role": "user", "content": message}]}

    while True:
        # 本轮"无工具调用的模型文本"= 回合结束时的最终回答候选；
        # 模型消息带 tool_calls 说明回合还会继续（工具/子智能体执行后回到模型）
        final_reply: str | None = None

        # astream 会持续产出模型节点、工具节点和子智能体节点的状态片段
        async for chunk in agent.astream(agent_input, config=config):
            # chunk 形如 {"model": {"messages": [...]}}，这里主要关心模型最新消息
            for node_name, state in chunk.items():
                if not state or "messages" not in state:
                    continue
                messages = state["messages"]
                if messages and isinstance(messages, list):
                    # 熔断检查放在处理消息之前：预算已超时立即停止，不再多消费一轮
                    if tokens_used > MODEL_TOKEN_RUN_LIMIT:
                        raise RuntimeError(
                            f"单次任务 token 消耗 {tokens_used} 已超过预算上限 "
                            f"{MODEL_TOKEN_RUN_LIMIT}，任务终止"
                        )
                    for msg in messages:
                        usage = getattr(msg, "usage_metadata", None)
                        if usage:
                            # total_tokens 缺失时按 input+output 兜底
                            tokens_used += usage.get("total_tokens") or (
                                usage.get("input_tokens", 0)
                                + usage.get("output_tokens", 0)
                            )
                    last_msg = messages[-1]
                    if node_name == "model":
                        if last_msg.tool_calls:
                            # DeepAgents 调用子智能体时，本质上会产生名为 task 的工具调用
                            for tool_call in last_msg.tool_calls:
                                if tool_call["name"] == "task":
                                    # 子智能体调用单独上报，前端可以展示“正在调用哪个专家助手”
                                    monitor.report_assistant(
                                        tool_call["args"]["subagent_type"],
                                        {
                                            "description": tool_call["args"][
                                                "description"
                                            ]
                                        },
                                    )
                            # 本轮仍在推进，不是收尾文本
                            final_reply = None
                        elif last_msg.content:
                            # 模型没有继续调用工具时，最新文本内容就是本轮可反馈给前端的结果
                            logger.info(
                                f"主智能体执行结果，最终结果：{last_msg.content[:100]}"
                            )
                            monitor.report_task_result(last_msg.content)
                            # 记录为最终回答候选，供回合结束后的守卫检查
                            final_reply = last_msg.content

        # 回合自然结束后执行最终回答守卫（判据与续跑提示词见 final_answer_guard）
        if (
            guard_remaining > 0
            and final_reply is not None
            and is_pure_transition_reply(final_reply)
        ):
            guard_remaining -= 1
            logger.warning(
                "[MainAgent] 检测到纯过渡语早停"
                f"（{str(final_reply)[:80]}），触发最终回答守卫强制续跑"
            )
            agent_input = {
                "messages": [{"role": "user", "content": FINAL_ANSWER_GUARD_PROMPT}]
            }
            continue
        break

    # 返回本次任务的实际 token 消耗（评测成本核算用；熔断之外的可观测性输出）
    return tokens_used


async def run_deep_agent(task_query, session_id, user_id="local"):
    """
    异步流式执行主智能体

    API 层会为每次任务传入用户问题、session_id 和租户 user_id。本函数负责
    准备租户隔离的会话目录、复制上传文件、写入 ContextVar，并在流式执行
    过程中把关键事件上报给前端。
    :param task_query: 前端提交的原始任务问题
    :param session_id: 当前任务 ID，同时用于输出目录和 WebSocket 定向推送
    :param user_id: 租户身份，目录、checkpointer 和推送路由都按其隔离
    """
    # 先落 trace/user 上下文，让本次任务从入口日志开始就携带 trace_id/user_id
    trace_id, trace_token = set_trace_context()
    user_token = set_user_context(user_id)

    logger.info(f"[MainAgent] 开始执行会话，user_id={user_id}，session_id={session_id}")

    # 每个租户每个会话独立使用 output/user_{uid}/session_{session_id}，
    # 避免不同用户的产物互相覆盖或互访
    session_dir = (
        project_root_path / "output" / f"user_{user_id}" / f"session_{session_id}"
    )
    session_dir.mkdir(parents=True, exist_ok=True)

    # 前端和工具使用绝对路径；提示词里只给模型相对路径，降低模型误用系统绝对路径的概率
    session_dir_str = str(session_dir).replace("\\", "/")
    relative_session_dir_str = str(session_dir.relative_to(project_root_path)).replace(
        "\\", "/"
    )

    # 上传文件先落在 updated/user_{uid}/session_{session_id}，执行前复制到本次 output 工作目录
    # 这样读文件工具和生成文件工具都只需要围绕同一个 session_dir 工作
    updated_dir_path = (
        project_root_path / "updated" / f"user_{user_id}" / f"session_{session_id}"
    )
    updated_info_prompt = ""
    if updated_dir_path.exists():
        files = [f.name for f in updated_dir_path.iterdir() if f.is_file()]
        if files:
            for filename in files:
                # copy2 会保留上传文件的修改时间、权限等元数据，便于后续排查文件来源
                shutil.copy2(updated_dir_path / filename, session_dir / filename)

            # 把上传文件列表注入用户消息，提醒模型先调用 read_file_content 获取附件内容
            updated_info_prompt = (
                "\n    [已上传文件] 已加载到工作目录:\n"
                + "\n".join([f"    - {f}" for f in files])
                + "\n    请优先使用工具（read_file_content）读取并参考这些文件。"
            )

    # 复合键与 server.composite_task_key 保持一致：
    # 1. WebSocket 按它路由 monitor 事件；2. LangGraph 按它隔离租户状态
    routing_key = f"{user_id}-{session_id}"

    # ContextVar 让深层工具无需显式传参，也能拿到当前会话目录和 WebSocket 路由键
    session_dir_token = set_session_context(session_dir_str)
    session_id_token = set_thread_context(routing_key)

    # 前端拿到工作目录后，可以展示本次任务生成的 Markdown/PDF 等产物
    monitor.report_session_dir(session_dir_str)

    # checkpointer 依赖 thread_id 区分会话记忆；复合键保证租户间状态隔离
    config = {"configurable": {"thread_id": routing_key}}

    # 实际 token 消耗（wait_for 超时/异常时保留已消耗部分；无调用则为 0）
    tokens_used = 0

    # 工作环境指令是运行时动态补充的，约束模型只在当前会话目录读写文件
    path_instruction = f"""
    【工作环境指令】
    工作目录: {relative_session_dir_str}
    {updated_info_prompt}

    规则：
    1. 新生成文件必须保存到工作目录：'{relative_session_dir_str}/filename'
    2. 读取已上传的文件时，请直接将文件名（例如：'开篇.txt'）作为 filename 参数传入（read_file_content）读取工具，不要带上任何目录前缀。
    3. 使用相对路径，禁止使用绝对路径
    4. 若存在上传文件，请先分析内容
    """

    try:
        # 硬超时兜底（P1-4）：LLM 网络半开或失控循环会让任务永远停在"运行中"。
        # 超时取消内部执行并经 monitor 告知前端；用户主动取消走 CancelledError 分支
        tokens_used = await asyncio.wait_for(
            _consume_agent_stream(
                _get_agent, task_query + path_instruction, config
            ),
            timeout=TASK_TIMEOUT_SECONDS,
        )

    except asyncio.TimeoutError:
        # wait_for 超时会先取消内部协程再抛 TimeoutError，此处告知前端并正常收尾
        monitor.report_error(
            f"任务执行超过 {TASK_TIMEOUT_SECONDS:.0f} 秒硬超时，已终止；"
            "可缩小任务范围后重试"
        )
    except asyncio.CancelledError:
        monitor.report_task_cancelled()
        raise
    except Exception as e:
        # 异步执行异常也走 monitor，保证前端能收到明确错误事件
        monitor.report_error(f"执行主智能发生异常信息：{str(e)}")
    finally:
        # 任务结束后恢复 ContextVar，避免后续请求复用到本次会话目录或 thread_id
        reset_session_context(
            session_dir_token, session_id_token, trace_token, user_token
        )

    # 返回实际 token 消耗（正常完成/超时/异常均尽力返回已消耗部分；
    # API 层不使用返回值，评测 runner 用它做单条成本核算）
    return tokens_used


if __name__ == "__main__":
    import asyncio

    asyncio.run(
        run_deep_agent(
            "从网络查询机器人信息，并生成Markdown文件",
            "test_session_001",
            user_id="local",
        )
    )
