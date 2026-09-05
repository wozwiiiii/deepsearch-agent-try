"""
文件产出意图硬约束测试（P2 文件生成劫持修复的工具层守卫）

背景：提示词层约束把"用户没要求生成文件但模型调用了 generate_markdown"
的劫持从基线 3 条降到 1 条（web-07），降低但未根除。工具层硬约束在
generate_markdown / convert_md_to_pdf 执行入口校验触发本次任务的用户
原始消息：不含明确文件产出意图则拒绝执行并返回引导信息。

测试分三层（全部 mock/本地文件，不发真实 LLM 请求，不产生费用）：
- has_file_output_intent 纯函数判据：正例（route-04 原句必须放行）、
  反例（web-07 劫持原句必须拒绝）、边界表述；
- ensure_file_output_requested 守卫：上下文缺失时保守放行（fail-open）；
- 工具入口集成：generate_markdown / convert_md_to_pdf 拒绝时零副作用
  （不落盘）且返回引导信息，放行时行为与现状一致。
"""

from pathlib import Path

from app.api.context import (
    get_user_message_context,
    reset_session_context,
    set_session_context,
    set_user_message_context,
)
from app.tools.file_intent_guard import (
    FILE_INTENT_REFUSAL_MESSAGE,
    ensure_file_output_requested,
    has_file_output_intent,
)
from app.tools.markdown_tools import generate_markdown
from app.tools.pdf_tools import convert_md_to_pdf


# ---------------------------------------------------------------------------
# 判据纯函数：正例（必须全部放行，误挡 = 评测回退）
# ---------------------------------------------------------------------------

def test_route04_exact_phrase_is_allowed():
    """评测用例 route-04 原句必须放行（被误挡会直接造成评测回退）"""
    assert (
        has_file_output_intent("根据查到的库存数据，生成一份 Markdown 格式的库存周报。")
        is True
    )


def test_more_positive_phrases_are_allowed():
    """其余典型文件产出表述全部放行"""
    positives = [
        "生成一份库存周报",           # 生成 + 周报
        "把查询结果导出为 PDF",        # 导出 + PDF
        "帮我保存这个文件",            # 保存 + 文件
        "写一份日报总结今天的进展",     # 写 + 日报
        "把数据整理成表格",            # 整理 + 表格
        "输出一份 Word 版分析文档",     # 输出 + Word/文档
        "生成md文件",                 # 中文与英文扩展名无空格拼接
        "保存为 md 格式",             # 英文词与中文混排的边界匹配
        "制作一份会议纪要",            # 制作 + 纪要
        "please export the report as pdf",  # 英文表述
        "把结果写成文档发给客户",       # 写 + 文档
    ]
    for text in positives:
        assert has_file_output_intent(text) is True, text


# ---------------------------------------------------------------------------
# 判据纯函数：反例（必须全部拒绝）
# ---------------------------------------------------------------------------

def test_web07_exact_phrase_is_rejected():
    """web-07 实际劫持场景原句必须拒绝"""
    assert has_file_output_intent("搜索质子泵抑制剂长期使用的潜在风险") is False


def test_more_negative_phrases_are_rejected():
    """无文件产出意图的查询表述全部拒绝"""
    negatives = [
        "把结果整理一下",             # 只有动作词，无文件词
        "搜索质子泵抑制剂长期使用的潜在风险",  # web-07：动作词、文件词均无
        "查一下库存最多的药品",         # 纯查询
        "总结一下这些风险",           # "总结"作动词用且无文件词
        "汇总一下各渠道的销售数据",     # 汇总但无文件词
        "",                           # 空文本
        "   ",                        # 纯空白
        None,                         # 非字符串
        123,
    ]
    for text in negatives:
        assert has_file_output_intent(text) is False, repr(text)


def test_action_alone_without_file_noun_is_rejected():
    """动作词与文件词必须同时出现：只有动作词不构成文件产出意图"""
    assert has_file_output_intent("帮我生成一下") is False
    assert has_file_output_intent("把答案写出来") is False


def test_file_noun_alone_without_action_is_rejected():
    """只有文件词（如'搜索关于报告写作的资料'）不构成文件产出意图"""
    assert has_file_output_intent("搜索关于年度报告的市场资料") is False
    assert has_file_output_intent("数据库里有哪些文件") is False


def test_english_extension_boundary_matching():
    """英文扩展名识别不被无关英文单词误触发"""
    # "command"/"md" 不应因子串误配……"command" 不含独立 md
    assert has_file_output_intent("run the command") is False
    # 独立出现的扩展名词可识别
    assert has_file_output_intent("convert it to MD please") is True


# ---------------------------------------------------------------------------
# 守卫函数：上下文缺失时保守放行（fail-open）
# ---------------------------------------------------------------------------

def test_guard_fails_open_when_context_missing():
    """脚本调试/直接调用场景拿不到用户消息：保守放行（防误伤优先）"""
    assert get_user_message_context() is None
    assert ensure_file_output_requested() is None


def test_guard_allows_when_intent_present():
    """用户消息含文件产出意图 → 放行（返回 None）"""
    session_token = set_session_context("/tmp/guard-dummy")
    message_token = set_user_message_context(
        "根据查到的库存数据，生成一份 Markdown 格式的库存周报。"
    )
    try:
        assert ensure_file_output_requested() is None
    finally:
        reset_session_context(session_token, message_token=message_token)


def test_guard_refuses_when_intent_absent():
    """用户消息不含文件产出意图 → 返回引导性拒绝信息"""
    session_token = set_session_context("/tmp/guard-dummy")
    message_token = set_user_message_context("搜索质子泵抑制剂长期使用的潜在风险")
    try:
        refusal = ensure_file_output_requested()
        assert refusal == FILE_INTENT_REFUSAL_MESSAGE
        assert "未要求生成文件" in refusal
        assert "直接在最终回答文本中" in refusal
    finally:
        reset_session_context(session_token, message_token=message_token)


# ---------------------------------------------------------------------------
# 工具入口集成：generate_markdown
# ---------------------------------------------------------------------------

def _set_session(session_dir, message):
    """同时设置会话目录与用户消息上下文，返回 (session_token, message_token)"""
    session_token = set_session_context(session_dir)
    message_token = set_user_message_context(message)
    return session_token, message_token


def test_generate_markdown_allows_route04_and_writes_file(tmp_path):
    """route-04 表述：守卫放行，工具正常生成文件（行为与现状一致）"""
    session_token, message_token = _set_session(
        str(tmp_path),
        "根据查到的库存数据，生成一份 Markdown 格式的库存周报。",
    )
    try:
        result = generate_markdown.invoke(
            {"content": "# 库存周报\n\n阿莫西林库存最多。", "filename": "库存周报"}
        )
        assert "已成功生成并保存" in result
        written = Path(result.split("'")[1])
        assert written.exists()
        assert "# 库存周报" in written.read_text(encoding="utf-8")
    finally:
        reset_session_context(session_token, message_token=message_token)


def test_generate_markdown_refuses_web07_without_side_effect(tmp_path):
    """web-07 表述：守卫拒绝，返回引导信息且不产生任何文件"""
    session_token, message_token = _set_session(
        str(tmp_path), "搜索质子泵抑制剂长期使用的潜在风险"
    )
    try:
        result = generate_markdown.invoke(
            {"content": "不应写入的内容", "filename": "不应存在.md"}
        )
        assert result == FILE_INTENT_REFUSAL_MESSAGE
        assert "未要求生成文件" in result
        assert "直接在最终回答文本中" in result
        assert not (tmp_path / "不应存在.md").exists()
    finally:
        reset_session_context(session_token, message_token=message_token)


def test_generate_markdown_boundary_phrases(tmp_path):
    """边界表述：'把结果整理一下'拒绝；含文件词的表述放行"""
    # 无文件词 → 拒绝
    session_token, message_token = _set_session(str(tmp_path), "把结果整理一下")
    try:
        result = generate_markdown.invoke(
            {"content": "x", "filename": "边界拒绝用例.md"}
        )
        assert result == FILE_INTENT_REFUSAL_MESSAGE
        assert not (tmp_path / "边界拒绝用例.md").exists()
    finally:
        reset_session_context(session_token, message_token=message_token)

    # 有文件词 → 放行（正常生成）
    session_token, message_token = _set_session(
        str(tmp_path), "请把结果输出成 Word 文档"
    )
    try:
        result = generate_markdown.invoke(
            {"content": "# 边界放行", "filename": "边界放行用例"}
        )
        assert "已成功生成并保存" in result
    finally:
        reset_session_context(session_token, message_token=message_token)


# ---------------------------------------------------------------------------
# 工具入口集成：convert_md_to_pdf
# ---------------------------------------------------------------------------

def test_convert_md_to_pdf_refuses_web07(tmp_path):
    """convert_md_to_pdf 同样受守卫约束：web-07 表述拒绝且不触碰文件系统"""
    session_token, message_token = _set_session(
        str(tmp_path), "搜索质子泵抑制剂长期使用的潜在风险"
    )
    try:
        result = convert_md_to_pdf.invoke({"md_filename": "不存在.md"})
        assert result == FILE_INTENT_REFUSAL_MESSAGE
    finally:
        reset_session_context(session_token, message_token=message_token)


def test_convert_md_to_pdf_allows_route04(tmp_path):
    """convert_md_to_pdf 放行路径零副作用：route-04 表述通过守卫，走到后续
    正常逻辑（源文件不存在时返回'文件不存在'业务错误而非拒绝信息——证明
    守卫已放行且未改变原有行为）"""
    session_token, message_token = _set_session(
        str(tmp_path),
        "根据查到的库存数据，生成一份 Markdown 格式的库存周报。",
    )
    try:
        result = convert_md_to_pdf.invoke({"md_filename": "库存周报.md"})
        assert result != FILE_INTENT_REFUSAL_MESSAGE
        assert "文件不存在" in result
    finally:
        reset_session_context(session_token, message_token=message_token)
