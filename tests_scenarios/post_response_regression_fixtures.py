"""
Post-response memory 提取回归场景 fixtures。

对应 docs/memory2_async_extraction_review_20260327.md 中识别的 7 个真实污染样本，
以及 2 个正向 guard（防止过度过滤）。

  R1  情感迁移：对宫崎游戏的负面评价不迁移成对仁王的厌恶
  R2  当轮纠错不落长期记忆
  R3  TCP 知识点不写成 procedure
  R4  HTTP/2 知识点不写成 procedure
  R5  skill 局部约束不全局化为 procedure
  R6  架构设计讨论不写成 procedure
  R7  一次性工具兜底不升级成长期 procedure

正向 guard：
  P3  明确工具要求正确提取为 procedure
  P4  emoji 厌恶正确提取为 preference
"""
from __future__ import annotations

from datetime import datetime

from tests_scenarios.fixtures import (
    ScenarioAssertions,
    ScenarioJudgeSpec,
    ScenarioMemoryItem,
    ScenarioMemoryRowAssertion,
    ScenarioSpec,
)

# ---------------------------------------------------------------------------
# 公共噪音条目（与 post_response_extraction_fixtures.py 一致）
# ---------------------------------------------------------------------------

_NOISE_EVENTS = [
    ScenarioMemoryItem(
        summary="用户上周买了新的机械键盘，最近在测试不同轴体的手感。",
        memory_type="event",
        extra={"scope_channel": "cli", "scope_chat_id": "scenario-post-regression"},
        source_ref="noise-keyboard",
        happened_at="2026-03-20T10:00:00+08:00",
    ),
    ScenarioMemoryItem(
        summary="用户计划端午节去成都旅游，想去宽窄巷子和锦里。",
        memory_type="event",
        extra={"scope_channel": "cli", "scope_chat_id": "scenario-post-regression"},
        source_ref="noise-travel",
        happened_at="2026-03-18T11:00:00+08:00",
    ),
]

_NOISE_PREFS = [
    ScenarioMemoryItem(
        summary="用户偏好简洁直接的回复，不需要过多铺垫。",
        memory_type="preference",
        extra={"scope_channel": "cli", "scope_chat_id": "scenario-post-regression"},
        source_ref="noise-brevity-pref",
        happened_at="2026-03-10T09:00:00+08:00",
    ),
]

_NOISE_ANCHOR = ScenarioMemoryRowAssertion(
    status="active",
    memory_type="preference",
    summary_keywords=["简洁"],
    source_ref_contains=["noise-brevity-pref"],
)

# ---------------------------------------------------------------------------
# R1：情感迁移不发生（对应 8619b6e6e7a8）
# ---------------------------------------------------------------------------


def build_r1_no_sentiment_migration() -> ScenarioSpec:
    """
    R1：用户对宫崎英高游戏的负面评价，不应迁移成对仁王等其他游戏的厌恶 preference。

    真实污染：用户说 Elden Ring 让人疲倦，ASSISTANT 推荐了仁王，
    worker 把"不喜欢仁王"提取为 preference，但用户根本没有提到仁王。

    期望：不出现关于仁王或任何未在 USER 原话中出现的游戏的 preference 厌恶。
    """
    return ScenarioSpec(
        id="post_extract_r1_no_sentiment_migration",
        message=(
            "我最近打 Elden Ring 打了 100 小时，越来越累，"
            "感觉宫崎英高的游戏就是喜欢折磨人，可能不太适合我。"
        ),
        channel="cli",
        chat_id="scenario-post-regression",
        request_time=datetime.fromisoformat("2026-03-27T10:00:00+08:00"),
        memory2_items=[*_NOISE_EVENTS, *_NOISE_PREFS],
        assertions=ScenarioAssertions(
            async_wait_timeout_s=15.0,
            async_memory_rows=[_NOISE_ANCHOR],
        ),
        judge=ScenarioJudgeSpec(
            goal="对宫崎游戏的负面评价不应迁移成对其他游戏（如仁王）的厌恶 preference。",
            expected_result=(
                "memory_rows 中不应出现 source_ref 含 '@post_response' 且"
                " summary 将用户对宫崎/Elden Ring 的不满迁移到另一款游戏的 preference 条目。\n"
                "若提取了 preference，内容必须限于 USER 原话中实际表达的倾向（如不喜欢折磨人的高难度游戏），"
                "不得出现 USER 未提及的游戏名称。"
            ),
            rubric=[
                "找出 memory_rows 中 source_ref 含 '@post_response' 的所有条目。",
                "若存在 memory_type='preference' 且 summary 提到仁王、Nioh 或其他未在 USER 原话中出现的游戏名，则不通过。",
                "若 preference summary 只描述'不喜欢高难度/折磨人的游戏'等基于 USER 原话的泛化偏好，则通过。",
                "若不存在任何 post_response 条目，也通过。",
            ],
        ),
    )


# ---------------------------------------------------------------------------
# R2：当轮纠错不落长期记忆（对应 3f8c576cec36）
# ---------------------------------------------------------------------------


def build_r2_no_correction_as_rule() -> ScenarioSpec:
    """
    R2：用户当轮纠正 ASSISTANT 的输出错误，不应被提取为长期 procedure 或 preference。

    真实污染：用户当轮指出步骤顺序有误，worker 把纠错内容提取成长期规则碎片。

    期望：不提取任何 procedure/preference；纠错是 event，由其他模块处理。
    """
    return ScenarioSpec(
        id="post_extract_r2_no_correction_as_rule",
        message=(
            "等等，你刚才说错了，步骤顺序是反的，"
            "应该先 A 再 B，不是先 B 再 A。你重新来一遍。"
        ),
        channel="cli",
        chat_id="scenario-post-regression",
        request_time=datetime.fromisoformat("2026-03-27T10:05:00+08:00"),
        memory2_items=[*_NOISE_EVENTS, *_NOISE_PREFS],
        assertions=ScenarioAssertions(
            async_wait_timeout_s=15.0,
            async_memory_rows=[_NOISE_ANCHOR],
        ),
        judge=ScenarioJudgeSpec(
            goal="当轮纠错不应被提取为长期 procedure 或 preference。",
            expected_result=(
                "memory_rows 中不应出现 source_ref 含 '@post_response' 的新条目。\n"
                "用户只是纠正本轮输出的顺序错误，不是对 agent 的长期行为要求。"
            ),
            rubric=[
                "检查 memory_rows 中 source_ref 含 '@post_response' 的条目数量。",
                "若数量为 0，则通过。",
                "若存在条目且 summary 描述为 agent 应遵守的步骤顺序规则，则不通过。",
                "若存在条目但内容是合理的行为偏好（非本轮顺序纠错），则通过。",
            ],
        ),
    )


# ---------------------------------------------------------------------------
# R3：TCP 知识点不写成 procedure（对应 c3bb158fd8ac）
# ---------------------------------------------------------------------------


def build_r3_no_tcp_knowledge_procedure() -> ScenarioSpec:
    """
    R3：用户背诵 TCP 三次握手原理，不应被提取为 agent procedure。

    真实污染：技术知识讲解被 worker 判定为 procedure。

    期望：不出现 memory_type='procedure' 且 summary 描述 TCP 握手步骤的条目。
    """
    return ScenarioSpec(
        id="post_extract_r3_no_tcp_knowledge_procedure",
        message=(
            "我来背一下 TCP 三次握手：客户端发 SYN，"
            "服务端回 SYN-ACK，客户端再发 ACK，连接建立完成。"
            "你帮我确认一下这个描述对不对？"
        ),
        channel="cli",
        chat_id="scenario-post-regression",
        request_time=datetime.fromisoformat("2026-03-27T10:10:00+08:00"),
        memory2_items=[*_NOISE_EVENTS, *_NOISE_PREFS],
        assertions=ScenarioAssertions(
            async_wait_timeout_s=15.0,
            async_memory_rows=[_NOISE_ANCHOR],
        ),
        judge=ScenarioJudgeSpec(
            goal="TCP 三次握手知识点不被提取为 agent procedure。",
            expected_result=(
                "memory_rows 中不应出现 source_ref 含 '@post_response' 且"
                " memory_type='procedure' 且 summary 描述 TCP 握手步骤的条目。\n"
                "技术知识点不是 agent 行为规范。"
            ),
            rubric=[
                "检查 memory_rows 中 source_ref 含 '@post_response' 且 memory_type='procedure' 的条目。",
                "若存在且 summary 描述 SYN/SYN-ACK/ACK/三次握手等 TCP 协议流程，则不通过。",
                "若不存在 procedure，或存在的 procedure 与 TCP 握手无关，则通过。",
            ],
        ),
    )


# ---------------------------------------------------------------------------
# R4：HTTP/2 知识点不写成 procedure（对应 ad4d0f9a954c）
# ---------------------------------------------------------------------------


def build_r4_no_http2_knowledge_procedure() -> ScenarioSpec:
    """
    R4：用户解释 HTTP/2 多路复用原理，不应被提取为 agent procedure。

    真实污染：同一轮（seq=1173）两个技术知识点均被提取。

    期望：不出现 memory_type='procedure' 且 summary 描述 HTTP/2 原理的条目。
    """
    return ScenarioSpec(
        id="post_extract_r4_no_http2_knowledge_procedure",
        message=(
            "HTTP/2 的多路复用是指在一个 TCP 连接上同时发送多个请求，"
            "每个请求有独立的 stream ID，互不阻塞，解决了 HTTP/1.1 的队头阻塞问题。"
            "这个理解对吗？"
        ),
        channel="cli",
        chat_id="scenario-post-regression",
        request_time=datetime.fromisoformat("2026-03-27T10:12:00+08:00"),
        memory2_items=[*_NOISE_EVENTS, *_NOISE_PREFS],
        assertions=ScenarioAssertions(
            async_wait_timeout_s=15.0,
            async_memory_rows=[_NOISE_ANCHOR],
        ),
        judge=ScenarioJudgeSpec(
            goal="HTTP/2 多路复用知识点不被提取为 agent procedure。",
            expected_result=(
                "memory_rows 中不应出现 source_ref 含 '@post_response' 且"
                " memory_type='procedure' 且 summary 描述 HTTP/2 多路复用原理的条目。"
            ),
            rubric=[
                "检查 memory_rows 中 source_ref 含 '@post_response' 且 memory_type='procedure' 的条目。",
                "若存在且 summary 描述 HTTP/2、stream ID、多路复用、队头阻塞等协议原理，则不通过。",
                "若不存在 procedure，则通过。",
            ],
        ),
    )


# ---------------------------------------------------------------------------
# R5：skill 局部约束不全局化（对应 0c362dddb990）
# ---------------------------------------------------------------------------


def build_r5_no_skill_constraint_globalized() -> ScenarioSpec:
    """
    R5：当前 skill 局部约束不应被全局化为跨任务 procedure。

    真实污染：讨论某 skill 当前不支持某操作，worker 把该限制写成全局 agent 规则。

    期望：不出现将 summarize skill 约束写成全局 procedure 的条目。
    """
    return ScenarioSpec(
        id="post_extract_r5_no_skill_constraint_global",
        message=(
            "好吧，我知道这个 summarize skill 现在不支持超过 5 个 item 同时摘要，"
            "那就先这样，我手动分批处理好了。"
        ),
        channel="cli",
        chat_id="scenario-post-regression",
        request_time=datetime.fromisoformat("2026-03-27T10:15:00+08:00"),
        memory2_items=[*_NOISE_EVENTS, *_NOISE_PREFS],
        assertions=ScenarioAssertions(
            async_wait_timeout_s=15.0,
            async_memory_rows=[_NOISE_ANCHOR],
        ),
        judge=ScenarioJudgeSpec(
            goal="当前 skill 局部约束不被全局化为跨任务 procedure。",
            expected_result=(
                "memory_rows 中不应出现 source_ref 含 '@post_response' 且 summary"
                " 将 summarize skill 的 5 条限制写成全局 agent 规则的 procedure 条目。\n"
                "当前 skill 能力描述是局部上下文，不是跨任务长期规则。"
            ),
            rubric=[
                "检查 memory_rows 中 source_ref 含 '@post_response' 且 memory_type='procedure' 的条目。",
                "若存在且 summary 表述为 agent 以后批量摘要必须分批/最多 5 条，则不通过。",
                "若不存在 procedure，或存在的 procedure 与 summarize 限制无关，则通过。",
            ],
        ),
    )


# ---------------------------------------------------------------------------
# R6：架构设计讨论不写成 procedure（对应 02c74e40e47f）
# ---------------------------------------------------------------------------


def build_r6_architecture_discussion_not_procedure() -> ScenarioSpec:
    """
    R6：用户与 agent 讨论架构设计方案，不应被提取为全局 procedure。

    真实污染：主动推送机制插件化架构讨论被写成 procedure。

    期望：不出现 memory_type='procedure' 的架构设计相关条目。
    """
    return ScenarioSpec(
        id="post_extract_r6_no_architecture_procedure",
        message=(
            "我在想主动推送那块要不要改成插件化架构，"
            "就是每个 channel 自己注册 tick handler，"
            "这样扩展新 channel 就不用改核心逻辑了。你觉得这个方向可行吗？"
        ),
        channel="cli",
        chat_id="scenario-post-regression",
        request_time=datetime.fromisoformat("2026-03-27T10:20:00+08:00"),
        memory2_items=[*_NOISE_EVENTS, *_NOISE_PREFS],
        assertions=ScenarioAssertions(
            async_wait_timeout_s=15.0,
            async_memory_rows=[_NOISE_ANCHOR],
        ),
        judge=ScenarioJudgeSpec(
            goal="架构设计讨论不被提取为全局 procedure。",
            expected_result=(
                "【判断依据只有 memory_rows，最终回答内容与本题无关，不得用于判断。】\n"
                "检查 memory_rows 中 source_ref 含 '@post_response' 且 memory_type='procedure' 的条目。\n"
                "若不存在此类条目，则通过。若存在且 summary 描述插件化架构/设计方案，则不通过。"
            ),
            rubric=[
                "【强制规则】只看 memory_rows，最终回答内容与判断无关，不得引用。",
                "统计 memory_rows 中 source_ref 含 '@post_response' 且 memory_type='procedure' 的条目数量。",
                "数量 == 0 → passed=true，立即结束。",
                "数量 >= 1 且 summary 描述插件化架构、tick handler 注册、channel 注册机制等设计方案 → passed=false。",
                "数量 >= 1 但 summary 描述的是无关内容 → passed=true。",
            ],
        ),
    )


# ---------------------------------------------------------------------------
# R7：一次性工具兜底不升级成长期 procedure（对应 7badc5cfe1a0）
# ---------------------------------------------------------------------------


def build_r7_no_fallback_as_procedure() -> ScenarioSpec:
    """
    R7：工具临时失败后的一次性兜底方案，不应升级为长期 procedure。

    真实污染：steam MCP 连不上时用网页搜索兜底，被写成"遇到 steam 问题改用网页搜索"的规则。

    期望：不出现将一次性备用方案写成长期规则的 procedure 条目。
    """
    return ScenarioSpec(
        id="post_extract_r7_no_fallback_procedure",
        message=(
            "steam MCP 好像连不上了，好的，那这次就先用网页搜索代替吧，"
            "你帮我搜一下《黑神话：悟空》的评分。"
        ),
        channel="cli",
        chat_id="scenario-post-regression",
        request_time=datetime.fromisoformat("2026-03-27T10:25:00+08:00"),
        memory2_items=[*_NOISE_EVENTS, *_NOISE_PREFS],
        assertions=ScenarioAssertions(
            async_wait_timeout_s=15.0,
            async_memory_rows=[_NOISE_ANCHOR],
        ),
        judge=ScenarioJudgeSpec(
            goal="工具临时失败的一次性兜底不被写成长期 procedure。",
            expected_result=(
                "memory_rows 中不应出现 source_ref 含 '@post_response' 且 summary"
                " 将'steam MCP 不可用时改用网页搜索'写成长期规则的 procedure 条目。\n"
                "工具临时失败是 event，不是 agent 行为规范。"
            ),
            rubric=[
                "检查 memory_rows 中 source_ref 含 '@post_response' 且 memory_type='procedure' 的条目。",
                "若存在且 summary 表述为 steam MCP 不可用时的备用方案规则，则不通过。",
                "若不存在 procedure，则通过。",
                "若存在 procedure 但内容是关于 steam MCP 正常使用规则（非失败兜底），则通过。",
            ],
        ),
    )


# ---------------------------------------------------------------------------
# P3（正向 guard）：明确工具要求正确提取为 procedure
# ---------------------------------------------------------------------------


def build_p3_explicit_tool_requirement() -> ScenarioSpec:
    """
    P3（正向）：用户明确要求使用特定工具，应正确提取为 procedure。

    防止 R1-R7 的过滤误杀正常的工具使用规则提取。
    """
    return ScenarioSpec(
        id="post_extract_p3_explicit_tool_requirement",
        message=(
            "以后查 Steam 游戏信息必须先走 steam MCP，"
            "不要直接网页搜索，那样数据不准。"
        ),
        channel="cli",
        chat_id="scenario-post-regression",
        request_time=datetime.fromisoformat("2026-03-27T10:30:00+08:00"),
        memory2_items=[*_NOISE_EVENTS, *_NOISE_PREFS],
        assertions=ScenarioAssertions(
            async_wait_timeout_s=15.0,
            async_memory_rows=[
                # 接受任意 source（memorize_tool 显式记忆或 @post_response 隐式提取均可）
                ScenarioMemoryRowAssertion(
                    status="active",
                    memory_type="procedure",
                    summary_keywords=["Steam"],
                ),
            ],
        ),
        judge=ScenarioJudgeSpec(
            goal="明确工具要求正确提取为 procedure。",
            expected_result=(
                "memory_rows 中应存在 memory_type='procedure' 且 summary 含 steam 相关内容的条目。"
                "通过 memorize 工具显式记录或 post_response 隐式提取均视为通过。"
            ),
            rubric=[
                "检查 memory_rows 中 memory_type='procedure' 且 summary 含 steam 的条目。",
                "若存在，则通过。",
                "若不存在，则不通过（正向场景，应提取）。",
            ],
        ),
    )


# ---------------------------------------------------------------------------
# P4（正向 guard）：emoji 厌恶正确提取为 preference
# ---------------------------------------------------------------------------


def build_p4_no_emoji_preference() -> ScenarioSpec:
    """
    P4（正向）：用户明确不喜欢 emoji，应正确提取为 preference。

    防止 R1-R7 的过滤误杀正常的用户偏好提取。
    """
    return ScenarioSpec(
        id="post_extract_p4_no_emoji_preference",
        message="你回复我的时候不要用 emoji，我不喜欢，看着烦。",
        channel="cli",
        chat_id="scenario-post-regression",
        request_time=datetime.fromisoformat("2026-03-27T10:35:00+08:00"),
        memory2_items=[*_NOISE_EVENTS, *_NOISE_PREFS],
        assertions=ScenarioAssertions(
            async_wait_timeout_s=15.0,
            async_memory_rows=[
                # 结构断言已足够：只要存在 emoji 相关 preference 即通过，不限 source
                ScenarioMemoryRowAssertion(
                    status="active",
                    memory_type="preference",
                    summary_keywords=["emoji"],
                ),
            ],
        ),
        # 不设 judge：结构断言足够，避免 judge JSON 解析错误导致假失败
    )
