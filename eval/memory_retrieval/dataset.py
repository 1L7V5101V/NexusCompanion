"""合成检索数据集（ADR-7）：固定种子、可复现、中文+英文混合。

结构：
- `_DOMAINS`：七个领域，每个领域一组主题短语，用于合成 summary 与 query 的
  确定性词面交叠。
- `build_dataset(seed, n_items, n_queries)`：用固定种子抽主题 → 生成记忆条目
  （summary + scope + 时间偏移）与检索查询（gold graded 0-3 + 干扰填充）。

relevance 分级（0-3）：
- 3：query 主题中的全部短语都出现在 summary
- 2：query 主题的至少一个短语出现在 summary
- 1：query 主题的核心词出现在 summary
- 0：不相关
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# 领域主题：领域名 -> (核心词, 主题短语集)。
_DOMAINS: list[tuple[str, tuple[str, ...]]] = [
    ("缓存", ("Redis 缓存配置", "本地缓存 失效 策略", "缓存 预热 冲击")),
    ("部署", ("K8s 集群 发布 回滚", "流水线 构建 产物", "灰度 上线 流量")),
    ("对账", ("支付流水 对账", "结算 差异 核对", "账单 汇总 一致性")),
    ("发票", ("发票 开票 冲红", "税收 抵扣 申报", "进项 销项 台账")),
    ("旅行", ("出行 机票 酒店", "行程 高铁 预订", "签证 行李 限额")),
    ("健康", ("体检 指标 复查", "运动 步数 心率", "睡眠 时长 质量")),
    ("饮食", ("餐厅 探店 打卡", "家庭 聚餐 菜谱", "咖啡 手冲 豆子")),
]

_EASTERN_PREFIXES = ("上周", "本月", "昨天", "去年", "最近")

# 与任何 query 词面低交叠的通用句子，用于把条目数补齐到 n_items。
_GENERIC_POOL = (
    "用户今天写了一段单元测试",
    "用户调整了日志输出级别",
    "用户给同事发了一封周报邮件",
    "用户整理了项目 TODO 清单",
    "用户更新了依赖版本",
    "用户清理了临时文件",
)


@dataclass(frozen=True)
class RetrievalItem:
    id: str
    summary: str
    domain: str
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalQuery:
    id: str
    text: str
    domain: str
    gold: dict[str, int]  # item_id -> graded relevance (0-3)


def build_dataset(
    *,
    seed: int = 202600901,
    n_items: int = 120,
    n_queries: int = 40,
) -> tuple[list[RetrievalItem], list[RetrievalQuery]]:
    rnd = random.Random(seed)

    # 1. 记忆条目：每领域生成主题核心条目 + 干扰变体。
    items: list[RetrievalItem] = []
    for domain, phrases in _DOMAINS:
        core_texts = [f"用户 {phrase} 的问题已解决" for phrase in phrases]
        # 干扰变体：核心词 + 一次相邻领域主题，拉低词面区分度。
        neighbor = _DOMAINS[(_DOMAINS.index((domain, phrases)) + 4) % len(_DOMAINS)][1][0]
        variants = [
            f"用户 {domain} 相关，最近处理过{neighbor}的事项",
            f"用户对 {domain} 有自己的偏好",
        ]
        for text in core_texts + variants:
            offset_days = rnd.randrange(0, 200)
            items.append(
                RetrievalItem(
                    id=f"i{len(items):03d}",
                    summary=text,
                    domain=domain,
                    extra={
                        "scope_channel": "tg",
                        "scope_chat_id": "1",
                        "_happened_at_offset_days": offset_days,
                    },
                )
            )

    # 补齐到 n_items：通用句子（词面低交叠）。
    while len(items) < n_items:
        items.append(
            RetrievalItem(
                id=f"i{len(items):03d}",
                summary=_GENERIC_POOL[rnd.randrange(len(_GENERIC_POOL))],
                domain="other",
                extra={
                    "scope_channel": "tg",
                    "scope_chat_id": "1",
                    "_happened_at_offset_days": rnd.randrange(0, 60),
                },
            )
        )

    # 2. 检索查询：gold = 所属领域核心条目，graded 由短语共享数决定。
    by_domain: dict[str, list[RetrievalItem]] = {}
    for item in items:
        by_domain.setdefault(item.domain, []).append(item)

    queries: list[RetrievalQuery] = []
    for qid in range(n_queries):
        domain, phrases = _DOMAINS[qid % len(_DOMAINS)]
        core_phrase = phrases[0]
        prefix = "如何"
        if qid % 5 == 0:
            prefix = _EASTERN_PREFIXES[qid % len(_EASTERN_PREFIXES)] + prefix
        text = f"{prefix}{core_phrase}？"

        gold: dict[str, int] = {}
        for item in by_domain.get(domain, []):
            grade = _grade_for(item, core_phrase, phrases)
            if grade > 0:
                gold[item.id] = grade
        queries.append(
            RetrievalQuery(
                id=f"q{qid:03d}",
                text=text,
                domain=domain,
                gold=gold,
            )
        )
    return items, queries


def _grade_for(item: RetrievalItem, core_phrase: str, phrases: tuple[str, ...]) -> int:
    """确定性 relevance：与 query 主题共享的短语越多，等级越高。"""
    summary = item.summary
    shared = sum(1 for phrase in phrases if phrase in summary)
    if shared >= 2:
        return 3
    if shared == 1:
        return 2
    if core_phrase.split()[0] in summary:
        return 1
    return 0


def domain_names() -> tuple[str, ...]:
    return tuple(domain for domain, _ in _DOMAINS)