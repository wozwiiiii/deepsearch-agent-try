"""
Tavily 搜索工具的上下文预算控制测试

背景（首轮基线 web-03 实测暴露）：搜索结果正文不做长度控制时，5 次检索
累计正文可把模型输入推过 Qwen2.5-32B 的 32768 上下文上限（39820 > 32768
报 400）。internet_search 现对 content/raw_content 做确定性截断。

测试不发真实网络请求：monkeypatch 模块级 tavily_client.search 返回假结果。
"""

import pytest

from app.api import monitor as monitor_mod
from app.tools import tavily_tool
from app.tools.tavily_tool import _MAX_CONTENT_CHARS, internet_search


@pytest.fixture(autouse=True)
def _quiet_monitor(monkeypatch):
    """隔离 monitor 副作用，专注验证截断逻辑"""
    monkeypatch.setattr(monitor_mod.monitor, "report_tool", lambda *a, **k: None)


def _fake_search(results):
    def search(**kwargs):
        return {"query": kwargs.get("query"), "results": results}

    return search


def test_long_content_truncated():
    """超过上限的 content 被截断并带标记"""
    long_body = "甲" * (_MAX_CONTENT_CHARS + 500)
    monkey_results = [{"title": "t", "url": "u", "content": long_body}]
    orig = tavily_tool.tavily_client.search
    tavily_tool.tavily_client.search = _fake_search(monkey_results)
    try:
        resp = internet_search.invoke({"query": "测试"})
    finally:
        tavily_tool.tavily_client.search = orig
    item = resp["results"][0]
    assert len(item["content"]) == _MAX_CONTENT_CHARS + len("…（超长截断）")
    assert item["content"].endswith("…（超长截断）")


def test_short_content_untouched():
    """不超长的结果原样透传，结构字段不变形"""
    monkey_results = [
        {"title": "指南", "url": "http://x", "content": "短内容", "score": 0.9}
    ]
    orig = tavily_tool.tavily_client.search
    tavily_tool.tavily_client.search = _fake_search(monkey_results)
    try:
        resp = internet_search.invoke({"query": "测试"})
    finally:
        tavily_tool.tavily_client.search = orig
    item = resp["results"][0]
    assert item["content"] == "短内容"
    assert item["title"] == "指南" and item["url"] == "http://x" and item["score"] == 0.9


def test_long_raw_content_truncated():
    """raw_content 超长同样被截断（include_raw_content=True 场景）"""
    monkey_results = [{"title": "t", "url": "u", "content": "ok",
                       "raw_content": "乙" * 5000}]
    orig = tavily_tool.tavily_client.search
    tavily_tool.tavily_client.search = _fake_search(monkey_results)
    try:
        resp = internet_search.invoke({"query": "测试", "include_raw_content": True})
    finally:
        tavily_tool.tavily_client.search = orig
    raw = resp["results"][0]["raw_content"]
    assert len(raw) < 5000 and raw.endswith("…（超长截断）")
