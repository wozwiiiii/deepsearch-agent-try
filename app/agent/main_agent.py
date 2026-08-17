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

from app.agent.llm import model
from app.agent.prompts import main_agent_content
from app.agent.subagents.database_query_agent import database_query_agent
from app.agent.subagents.knowledge_base_agent import knowledge_base_agent
from app.agent.subagents.network_search_agent import network_search_agent
from app.api.context import (
    reset_session_context,
    set_session_context,
    set_thread_context,
)
from app.api.monitor import monitor

# 文件类工具由主智能体直接掌握，负责读取上传附件和生成最终交付文档
from app.tools.markdown_tools import generate_markdown
from app.tools.pdf_tools import convert_md_to_pdf
from app.tools.upload_file_read_tool import read_file_content

# 当前文件位于 app/agent/main_agent.py，parents[1] 即 app 目录
project_root_path = Path(__file__).parents[1].resolve()

# 检查点数据库路径：默认 app/data/checkpoints.sqlite3，可用 CHECKPOINT_DB 覆盖
CHECKPOINT_DB = os.getenv(
    "CHECKPOINT_DB", str(project_root_path / "data" / "checkpoints.sqlite3")
)

# 模型调用次数硬上限（第三批成本治理）：
# - run_limit 限制单次任务内的模型调用总数（主智能体 + 子智能体各自循环都计数），
#   防止提示注入或规划失控导致无限烧 token；
# - thread_limit 限制同一复合 thread_id 的累计调用数（checkpointer 持久化后
#   会话可以跨重启续聊，长会话也需要总量上限）。
# 默认值按"复杂研究任务约 3 个子智能体 × 10-20 轮规划"量级设定，可用环境变量调整
MODEL_RUN_LIMIT = int(os.getenv("MODEL_RUN_LIMIT", "80"))
MODEL_THREAD_LIMIT = int(os.getenv("MODEL_THREAD_LIMIT", "300"))

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


async def _get_agent():
    """
    返回可复用的主智能体实例，首次调用时完成组装和持久化 checkpointer 初始化
    """
    global _main_agent, _checkpoint_saver
    if _main_agent is not None:
        return _main_agent

    async with _agent_init_lock:
        if _main_agent is not None:
            return _main_agent

        db_path = Path(CHECKPOINT_DB)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # from_conn_string 返回异步上下文管理器；这里显式进入并保持到进程结束，
        # 连接随进程退出由操作系统回收（SQLite WAL 下中断是安全的）
        saver_ctx = AsyncSqliteSaver.from_conn_string(str(db_path))
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
        print(
            f"[MainAgent] 已初始化，checkpointer=sqlite:{db_path}，"
            f"模型调用上限 run={MODEL_RUN_LIMIT}/thread={MODEL_THREAD_LIMIT}"
        )
        return _main_agent


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
    print(f"[MainAgent] 开始执行会话，user_id={user_id}，session_id={session_id}")

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
        # 首次调用会完成 Agent 组装和 SQLite checkpointer 初始化
        agent = await _get_agent()

        # astream 会持续产出模型节点、工具节点和子智能体节点的状态片段
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": task_query + path_instruction}]},
            config=config,
        ):
            # chunk 形如 {"model": {"messages": [...]}}，这里主要关心模型最新消息
            for node_name, state in chunk.items():
                if not state or "messages" not in state:
                    continue
                messages = state["messages"]
                if messages and isinstance(messages, list):
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
                        elif last_msg.content:
                            # 模型没有继续调用工具时，最新文本内容就是本轮可反馈给前端的结果
                            print(
                                f"主智能体执行结果，最终结果：{last_msg.content[:100]}"
                            )
                            monitor.report_task_result(last_msg.content)

    except asyncio.CancelledError:
        monitor.report_task_cancelled()
        raise
    except Exception as e:
        # 异步执行异常也走 monitor，保证前端能收到明确错误事件
        monitor.report_error(f"执行主智能发生异常信息：{str(e)}")
    finally:
        # 任务结束后恢复 ContextVar，避免后续请求复用到本次会话目录或 thread_id
        reset_session_context(session_dir_token, session_id_token)


if __name__ == "__main__":
    import asyncio

    asyncio.run(
        run_deep_agent(
            "从网络查询机器人信息，并生成Markdown文件",
            "test_session_001",
            user_id="local",
        )
    )
