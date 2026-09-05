"""
大模型初始化模块

负责从 .env 中读取模型配置，并创建项目统一复用的模型对象
后续主智能体和子智能体都从这里导入 model，避免在多个文件里重复加载环境变量
"""

import os

from dotenv import find_dotenv, load_dotenv
from langchain.chat_models import init_chat_model

# find_dotenv 会从当前目录向上查找 .env，适合脚本和 Web 服务从不同入口启动的场景
load_dotenv(find_dotenv())

# 使用 OpenAI 兼容协议接入模型；具体模型名由 .env 中的 LLM_QWEN_MAX 控制
# max_retries=2：底层 ChatOpenAI 仅在连接失败/可重试错误时自动重试（指数退避），
# 避免网络半开等瞬时故障把同步调用线程永久挂起。.env 仅有 qwen-max 一个端点，
# 无第二可降级模型，故只加重试不做 fallback
model = init_chat_model(
    model=os.getenv("LLM_QWEN_MAX"),
    model_provider="openai",
    max_retries=2,
)
