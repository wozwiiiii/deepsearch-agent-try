"""
pytest 全局配置

在导入任何 app 模块之前注入占位密钥：tavily_tool / llm 在模块级就会创建
客户端对象，缺少环境变量会直接 import 失败。占位密钥只让对象构造成功，
测试不会发起真实网络请求。
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "test-key-placeholder")
os.environ.setdefault("LLM_QWEN_MAX", "test-model-placeholder")
os.environ.setdefault("TAVILY_API_KEY", "test-key-placeholder")

# 保证从项目根目录（tests 的上一级）导入 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
