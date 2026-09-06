"""
最终回答守卫：拦截"纯过渡语早停"与"空输出早停"（P1 评测修复）

背景：主智能体的 ReAct 循环里，模型输出"不带工具调用的纯文本"即宣告回合
结束。评测三轮全量跑批（45 用例 × 3 轮 = 135 次运行）中出现两类早停签名：
1. 纯过渡语（基线/v2 共 5 次）：模型在单次调用后以"我将启动数据库查询
   助手进行查询。""我将等待其结果。"这类纯过渡语收尾，用户只收到一句
   没有任何实质内容的空话；
2. 空输出（v3 的 route-02/multi-07，n=2）：模型最终消息 content 为空，
   用户收到空回答、零工具调用、~8.2K tokens、无任何错误。

本模块提供守卫的判据与续跑提示词：
- is_pure_transition_reply 判定最终回答是否为"纯过渡语"。判据刻意保守
  （高精度、低召回），三条同时满足才判早停，避免误伤正常的简短实质回答：
    1. 是非空短文本（< FINAL_ANSWER_MAX_CHARS 字符）；
    2. 不含任何数字——"数据库里共有 50 种药品"这类一句话实质答案必然
       携带数据，直接放行；
    3. 含未来时/进行时过渡语短语（我将/让我/正在/等待/即将/请稍候…）。
- guard_trigger_reason 是守卫触发总判定入口，覆盖上述两类变体
  （空输出无需特征匹配——正常 ReAct 结束必经过文本轮，回合结束却无
  任何文本产出本身就是异常）。
- FINAL_ANSWER_GUARD_PROMPT 是守卫触发后注入会话的纠偏消息，要求模型
  要么真实调用工具/子智能体，要么基于已有结果给出实质回答。

触发频率与循环结构见 main_agent._consume_agent_stream：守卫在单个任务内
最多触发 FINAL_ANSWER_GUARD_MAX 次，续跑后仍命中早停特征则放行结束
（防死循环）。

调用上限边界（如实说明）：ModelCallLimitMiddleware 的 run 级上限
（MODEL_RUN_LIMIT=80）以 UntrackedValue 计数、不跨 astream 运行持久化，
守卫续跑是第二次独立 astream，run 级计数从 0 重新开始——单任务模型调用
实际上限最多放宽到约 2×run_limit；thread 级上限（MODEL_THREAD_LIMIT=300，
随 checkpoint 持久化）、600s 任务硬超时与 token 熔断（tokens_used 为
局部变量，跨守卫轮累计）仍然有效，作为成本兜底。

漏判边界（有意为之，"宁漏判不误伤"）：带阿拉伯/全角数字的过渡语
（如"稍等 5 秒"）被判据 2 放行、不触发守卫；不含过渡语短语的结论性
短句同样放行。误伤的代价是打断正常回答收尾，漏判的代价只是早停多发生
一次、且至多多消耗一次模型调用，故边界情形一律放行。
"""

# 最终回答长度上限（字符）：超过视为完整回答，不做拦截
FINAL_ANSWER_MAX_CHARS = 200

# 守卫在单个任务内最多触发次数：第二次仍输出过渡语则放行结束（防死循环）
FINAL_ANSWER_GUARD_MAX = 1

# 过渡语短语表：命中任一且满足其余判据才判早停。
# 只收未来时/进行时的"行动待发生"表达，不收结论性表达，保证精度
TRANSITIONAL_PHRASES: tuple[str, ...] = (
    # 第一人称未来意图
    "我将", "我将要", "我会", "让我", "让我来",
    # 进行中
    "正在", "查询中", "检索中", "处理中", "执行中",
    # 等待类
    "等待", "稍等", "请稍候", "请稍等",
    # 即将行动
    "即将", "马上", "这就",
)

# 守卫触发后注入会话的纠偏消息。空泛回答本身已在会话历史里
# （checkpointer 持有上下文），这里直接要求模型继续完成任务
FINAL_ANSWER_GUARD_PROMPT = (
    "【系统提醒】你上一条回复只是一句过渡说明，没有完成任务本身。"
    "请立即继续执行当前任务：需要数据或资料时，先真实调用对应的工具或"
    "子智能体获取结果，再基于获取到的结果给出包含具体内容的完整最终回答；"
    "如果所需信息已经获取，请直接给出实质回答。"
    "不要只回复“我将……”“正在查询……”之类的过渡语。"
)


def is_pure_transition_reply(text: object) -> bool:
    """
    判定一段最终回答是否为"纯过渡语"（早停特征）

    三条判据必须同时满足（详见模块 docstring）。判据刻意保守：
    误伤一条实质回答的代价是打断正常收尾，而漏判的代价只是早停多发生
    一次、且守卫触发后至多多消耗一次模型调用，因此边界情形一律放行。

    :param text: 模型最终消息的 content（非字符串形态一律放行）
    :return: True 表示命中"纯过渡语"特征，应触发守卫续跑
    """
    if not isinstance(text, str):
        # content 可能是内容块列表等非字符串形态：保守放行
        return False

    reply = text.strip()
    if not reply or len(reply) >= FINAL_ANSWER_MAX_CHARS:
        # 空文本放行；长文本视为完整回答，不做拦截
        return False

    # 含任何数字（含全角）即视为可能携带数据/结论，放行
    if any(ch.isdigit() for ch in reply):
        return False

    return any(phrase in reply for phrase in TRANSITIONAL_PHRASES)


def guard_trigger_reason(final_reply: object, ended_with_tool_calls: bool = False) -> str | None:
    """
    守卫触发总判定：回合结束时应否强制续跑；返回触发原因，None 表示放行

    :param final_reply: 回合结束时的最终回答候选（无任何文本产出时为 None）
    :param ended_with_tool_calls: 回合是否以带 tool_calls 的模型消息收尾。
        正常 ReAct 图不会在这种状态下 END（END 条件即"无工具调用的文本输出"），
        astream 以工具片段结束属流异常截断——图仍处于推进中间态，续跑可能
        重复执行工具，故守卫不介入（与既有测试 test_tool_call_only_end_
        does_not_trigger_guard 的设计意图一致）。

    覆盖两个实证的早停变体（见 EVAL_COMPARISON_V3_2026-09-06.md）：
    1. "transition_only"——纯过渡语（v3 实战 2 次触发 2 次救回）；
    2. "empty_output"——空输出（route-02/multi-07，n=2）：模型文本轮
       content 为空、无 tool_calls，用户收到空回答。回合结束时既无文本
       产出、也不在工具调用中间态，即为异常早停。
    """
    if final_reply is None or (
        isinstance(final_reply, str) and not final_reply.strip()
    ):
        # 无文本产出（None 或空白串）
        if ended_with_tool_calls:
            return None  # 工具片段截断：中间态，不介入
        return "empty_output"
    if is_pure_transition_reply(final_reply):
        return "transition_only"
    return None
