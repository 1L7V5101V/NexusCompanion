from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

@dataclass
class ScenarioMemorySeed:
    long_term: str = ""
    self_profile: str = ""
    now: str = ""


@dataclass
class ScenarioMemoryItem:
    summary: str
    memory_type: str
    extra: dict = field(default_factory=dict)
    source_ref: str = ""
    happened_at: str = ""


@dataclass
class ScenarioAssertions:
    route_decision: str | None = None
    min_history_hits: int | None = None
    max_history_hits: int | None = None
    required_tools: list[str] = field(default_factory=list)
    final_contains: list[str] = field(default_factory=list)
    final_not_contains: list[str] = field(default_factory=list)
    async_memory_rows: list["ScenarioMemoryRowAssertion"] = field(default_factory=list)
    async_wait_timeout_s: float = 0.0


@dataclass
class ScenarioJudgeSpec:
    goal: str
    expected_result: str = ""
    rubric: list[str] = field(default_factory=list)


@dataclass
class ScenarioMemoryRowAssertion:
    status: str
    summary_keywords: list[str] = field(default_factory=list)
    memory_type: str | None = None


@dataclass
class ScenarioSpec:
    id: str
    message: str
    channel: str
    chat_id: str
    request_time: datetime
    session_key: str = ""
    history: list[dict] = field(default_factory=list)
    memory: ScenarioMemorySeed = field(default_factory=ScenarioMemorySeed)
    memory2_items: list[ScenarioMemoryItem] = field(default_factory=list)
    assertions: ScenarioAssertions = field(default_factory=ScenarioAssertions)
    judge: ScenarioJudgeSpec | None = None

    @property
    def derived_session_key(self) -> str:
        return f"{self.channel}:{self.chat_id}"

    def validate_session_key(self) -> None:
        if self.session_key and self.session_key != self.derived_session_key:
            raise ValueError(
                "ScenarioSpec.session_key 与 channel/chat_id 推导结果不一致: "
                f"explicit={self.session_key} derived={self.derived_session_key}"
            )


def build_tool_search_schedule_scenario() -> ScenarioSpec:
    return ScenarioSpec(
        id="tool_search_schedule_real_tools",
        message="帮我十分钟后提醒喝水",
        channel="cli",
        chat_id="scenario-tool-search",
        session_key="cli:scenario-tool-search",
        request_time=datetime.fromisoformat("2026-03-12T10:00:00+08:00"),
        assertions=ScenarioAssertions(
            required_tools=["tool_search", "schedule"],
            final_contains=["提醒"],
        ),
    )


def build_smalltalk_no_retrieve_scenario() -> ScenarioSpec:
    return ScenarioSpec(
        id="smalltalk_no_retrieve_real",
        message="今天天气不错，我刚泡了杯茶，感觉还行。",
        channel="cli",
        chat_id="scenario-smalltalk",
        session_key="cli:scenario-smalltalk",
        request_time=datetime.fromisoformat("2026-03-12T10:05:00+08:00"),
        history=[
            {
                "role": "user",
                "content": "我昨晚有点累，不过今天已经好多了。",
                "timestamp": "2026-03-01T12:00:00+08:00",
            },
            {
                "role": "assistant",
                "content": "那就好，今天可以轻松一点。",
                "timestamp": "2026-03-01T12:00:10+08:00",
            },
        ],
        memory=ScenarioMemorySeed(
            long_term="用户长期偏好：喜欢轻松聊天，不喜欢太正式的回复。",
        ),
        memory2_items=[
            ScenarioMemoryItem(
                summary="用户偏好轻松聊天风格，不喜欢太正式的回复。",
                memory_type="preference",
                extra={"scope_channel": "cli", "scope_chat_id": "scenario-smalltalk"},
                source_ref="scenario-smalltalk-pref",
                happened_at="2026-03-01T12:00:00+08:00",
            )
        ],
        assertions=ScenarioAssertions(
            route_decision="NO_RETRIEVE",
            max_history_hits=0,
        ),
    )


def build_rag_with_noise_scenario() -> ScenarioSpec:
    return ScenarioSpec(
        id="rag_with_related_and_irrelevant_noise",
        message="我之前提过最近最上头的是哪款游戏吗？直接说名字。",
        channel="cli",
        chat_id="scenario-rag-noise",
        session_key="cli:scenario-rag-noise",
        request_time=datetime.fromisoformat("2026-03-12T10:10:00+08:00"),
        history=[
            {
                "role": "user",
                "content": "最近我还是喜欢那种高难度、能反复练习的动作游戏。",
                "timestamp": "2026-03-06T20:00:00+08:00",
            },
            {
                "role": "assistant",
                "content": "明白，你更偏向硬核动作游戏，不是纯剧情向。",
                "timestamp": "2026-03-06T20:00:10+08:00",
            },
        ],
        memory=ScenarioMemorySeed(
            long_term="用户喜欢直接回答，不要铺垫太多。",
        ),
        memory2_items=[
            ScenarioMemoryItem(
                summary="用户最近最上头的游戏是《仁王2》，这周连着玩了好几晚。",
                memory_type="event",
                extra={"scope_channel": "cli", "scope_chat_id": "scenario-rag-noise"},
                source_ref="scenario-rag-noise-correct",
                happened_at="2026-03-05T22:30:00+08:00",
            ),
            ScenarioMemoryItem(
                summary="用户去年很喜欢《艾尔登法环》，地图探索体验很好。",
                memory_type="event",
                extra={"scope_channel": "cli", "scope_chat_id": "scenario-rag-noise"},
                source_ref="scenario-rag-noise-old-like",
                happened_at="2025-11-10T21:00:00+08:00",
            ),
            ScenarioMemoryItem(
                summary="用户朋友最近在玩《只狼》，还推荐过义手打法。",
                memory_type="event",
                extra={"scope_channel": "cli", "scope_chat_id": "scenario-rag-noise"},
                source_ref="scenario-rag-noise-friend",
                happened_at="2026-03-04T19:00:00+08:00",
            ),
            ScenarioMemoryItem(
                summary="用户偏好高难度动作游戏，享受反复练习后的正反馈。",
                memory_type="profile",
                extra={"scope_channel": "cli", "scope_chat_id": "scenario-rag-noise"},
                source_ref="scenario-rag-noise-profile",
                happened_at="2026-02-20T18:00:00+08:00",
            ),
            ScenarioMemoryItem(
                summary="用户上周买了手冲咖啡壶，最近在试不同的滤杯。",
                memory_type="event",
                extra={"scope_channel": "cli", "scope_chat_id": "scenario-rag-noise"},
                source_ref="scenario-rag-noise-coffee",
                happened_at="2026-03-02T09:00:00+08:00",
            ),
            ScenarioMemoryItem(
                summary="用户计划下个月去杭州玩，正在看西湖附近酒店。",
                memory_type="event",
                extra={"scope_channel": "cli", "scope_chat_id": "scenario-rag-noise"},
                source_ref="scenario-rag-noise-travel",
                happened_at="2026-03-01T11:00:00+08:00",
            ),
            ScenarioMemoryItem(
                summary="用户最近在研究番茄炒蛋做法，想试试先炒蛋还是先炒番茄。",
                memory_type="event",
                extra={"scope_channel": "cli", "scope_chat_id": "scenario-rag-noise"},
                source_ref="scenario-rag-noise-cooking",
                happened_at="2026-03-03T12:00:00+08:00",
            ),
        ],
        assertions=ScenarioAssertions(
            route_decision="RETRIEVE",
            min_history_hits=1,
            final_contains=["仁王2"],
            final_not_contains=["只狼", "手冲咖啡", "西湖", "番茄炒蛋"],
        ),
    )


def build_async_memory_correction_scenario() -> ScenarioSpec:
    return ScenarioSpec(
        id="async_memory_correction_supersedes_old_rule",
        message=(
            "你之前关于查 Steam 的流程是错的。"
            "正确做法是：查 Steam 信息时必须先用 steam MCP，"
            "不能直接用 web_search。"
        ),
        channel="cli",
        chat_id="scenario-memory-correction",
        session_key="cli:scenario-memory-correction",
        request_time=datetime.fromisoformat("2026-03-12T10:15:00+08:00"),
        history=[
            {
                "role": "user",
                "content": "之前你查 Steam 信息时是怎么做的？",
                "timestamp": "2026-03-10T18:00:00+08:00",
            },
            {
                "role": "assistant",
                "content": "我会直接 web_search 查一下就行。",
                "timestamp": "2026-03-10T18:00:10+08:00",
            },
        ],
        memory2_items=[
            ScenarioMemoryItem(
                summary="查 Steam 信息时必须直接使用 web_search，不能先用 steam MCP。",
                memory_type="procedure",
                extra={
                    "steps": ["直接 web_search 查询 Steam 信息"],
                    "tool_requirement": "web_search",
                },
                source_ref="scenario-memory-correction-old-rule",
                happened_at="2026-03-09T20:00:00+08:00",
            )
        ],
        assertions=ScenarioAssertions(
            async_wait_timeout_s=12.0,
            async_memory_rows=[
                ScenarioMemoryRowAssertion(
                    status="superseded",
                    memory_type="procedure",
                    summary_keywords=["Steam", "web_search", "不能", "MCP"],
                ),
                ScenarioMemoryRowAssertion(
                    status="active",
                    memory_type="procedure",
                    summary_keywords=["Steam", "MCP", "必须"],
                ),
            ],
        ),
        judge=ScenarioJudgeSpec(
            goal="判断异步记忆纠错是否在业务语义上成立。",
            expected_result=(
                "旧的错误 Steam 查询规则应被淘汰；"
                "新的规则应明确表达“查 Steam 必须先用 steam MCP，不能直接用 web_search”。"
            ),
            rubric=[
                "结合用户原始纠正消息，判断 active 的新 procedure 是否忠实表达了新规则。",
                "判断 superseded 的旧 procedure 是否确实是被新规则取代的错误旧规则。",
                "若新规则缺少“必须先用 steam MCP”或缺少“不能直接用 web_search”，则不通过。",
                "重点根据 memory rows 判断，不要因为最终回答措辞保守、追问或承认冲突而直接判失败。",
                "若新旧状态与语义都成立，则通过。",
            ],
        ),
    )


def build_sample_scenarios(root: Path | None = None) -> list[ScenarioSpec]:
    _ = root
    return [
        build_tool_search_schedule_scenario(),
        build_smalltalk_no_retrieve_scenario(),
        build_rag_with_noise_scenario(),
        build_async_memory_correction_scenario(),
    ]
