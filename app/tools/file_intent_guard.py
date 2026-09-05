"""
文件产出意图硬约束（P2 文件生成劫持修复的工具层守卫）

背景：提示词层约束（prompts.yml「唯一标准」规则）把文件生成劫持从基线 3 条
降到 1 条（web-07），降低但未根除。提示词对模型行为倾向只能"劝"，本模块
在工具执行入口提供"硬约束"：触发本次任务的用户原始消息若不含明确的文件
产出意图，则拒绝执行文件生成类工具，并返回一条引导性错误信息，让模型转而
在最终回答文本中直接给出结论。

判定规则（宽进严出，防误伤）：
- 双条件同时满足才视为"用户要求生成文件"：消息中出现「动作词」（生成/导出/
  保存/写入/制作/整理…）**且**出现「文件词」（文件/文档/报告/周报/Markdown/
  PDF/Word…）；
- 评测用例 route-04「根据查到的库存数据，生成一份 Markdown 格式的库存周报。」
  必须放行（生成 + Markdown/周报 均命中）；
- web-07 劫持场景「搜索质子泵抑制剂长期使用的潜在风险」必须拒绝（两类词
  均未命中）；
- 上下文缺失（脚本调试、工具直接调用等拿不到用户消息的场景）时保守放行
  （fail-open），并记录 warning 留痕——防误伤优先，此时仍有提示词层约束兜底。

用户原始消息通过 app.api.context 的 ContextVar 注入（run_deep_agent 写入原始
task_query，不含运行时拼接的工作环境指令），工具层免层层传参即可读取。
"""

import re
from typing import Optional

from app.api.context import get_user_message_context
from app.utils.logging_setup import get_logger

logger = get_logger(__name__)

# 拒绝时返回给模型的引导信息：明确拒绝原因 + 指出正确行为
FILE_INTENT_REFUSAL_MESSAGE = (
    "错误：本次文件生成调用已被拒绝。触发当前任务的用户消息中没有明确的"
    "文件产出意图——用户并未要求生成文件。请不要再次调用文件生成类工具，"
    "直接在最终回答文本中完整给出结论和答案。"
)

# 动作词：表达"产出/落盘"行为的词汇（中英文）。命中任一即视为动作条件满足
_ACTION_PATTERN = re.compile(
    r"("
    r"生成|导出|保存|另存|输出|写入|写出|写成|制作|创建|新建|打印|"
    r"落盘|打包|转换|转成|转为|转化|整理|汇总|归档|做|写|"
    r"export|save|generate|write|convert|create|produce"
    r")",
    re.IGNORECASE,
)

# 文件词（中文）：具体的文件产出物。中文前后无需词边界，直接子串匹配
_CHINESE_FILE_NOUN_PATTERN = re.compile(
    r"(文件|文档|报告|周报|日报|月报|年报|纪要|总结|笔记|表格|清单|"
    r"说明书|手册|简历|方案)"
)

# 文件词（英文扩展名/格式词）：用 lookaround 代替 \b 做边界——中文与英文
# 字母在 Python re 中同为 \w，"生成md文件"这种无空格拼接用 \b 会漏配
_ENGLISH_FILE_NOUN_PATTERN = re.compile(
    r"(?i)(?<![a-z])(markdown|md|pdf|word|docx?|excel|csv|xlsx?|pptx?|txt)(?![a-z])"
)


def has_file_output_intent(text: object) -> bool:
    """
    判断用户消息是否包含明确的文件产出意图

    判据：动作词与文件词同时出现（宽进严出：只要求两类词各自命中一次，
    不约束相对位置，避免把"生成一份 Markdown 格式的库存周报"这类自然
    表述误判为拒绝）。

    :param text: 用户原始消息文本；非字符串或空文本一律视为无意图
    :return: True 表示消息明确要求产出文件，工具放行；False 表示应拒绝
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    has_action = bool(_ACTION_PATTERN.search(stripped))
    if not has_action:
        return False
    has_file_noun = bool(
        _CHINESE_FILE_NOUN_PATTERN.search(stripped)
        or _ENGLISH_FILE_NOUN_PATTERN.search(stripped)
    )
    return has_file_noun


def ensure_file_output_requested() -> Optional[str]:
    """
    文件生成类工具的入口守卫：校验当前任务的用户消息是否要求产出文件

    - 用户消息可从 ContextVar 拿到且不含文件产出意图 → 返回拒绝信息，
      工具应直接把该信息作为工具结果返回（模型收到后会转而在回答文本中
      直接给结论），同时 logger.warning 留痕；
    - 用户消息含文件产出意图 → 返回 None，工具按原逻辑继续执行（零副作用）；
    - 上下文缺失（脚本调试/直接调用场景，get_user_message_context() 为
      None）→ 无法判定，保守放行并记录 warning，防误伤优先。

    :return: 拒绝信息字符串（应拒绝并返回给模型），或 None（放行）
    """
    user_message = get_user_message_context()
    if user_message is None:
        logger.warning(
            "[FileIntentGuard] 未注入用户消息上下文（脚本调试/直接调用场景），"
            "无法判定文件产出意图，保守放行"
        )
        return None

    if has_file_output_intent(user_message):
        return None

    logger.warning(
        "[FileIntentGuard] 拒绝文件生成调用：用户消息不含文件产出意图，"
        f"用户消息: {user_message[:80]}"
    )
    return FILE_INTENT_REFUSAL_MESSAGE
