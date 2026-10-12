"""
proactive/memory_optimizer.py — 记忆质量优化器

每轮运行两步，都落在**该租户**的画像存储上（c18 task 6.11 / 6.10）：
  1. 把 pending 事实合并进 long_term，并在同一个事务里取走这些行
  2. 更新自我认知（relationship_state，经 persona 那条缝，单事务 + 审计）
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from agent.plugins.snapshot import RuntimeSnapshotStore

from agent.memory import DEFAULT_SELF_MD
from agent.provider import LLMProvider
from core.memory.profile_store import ProfileStoreResolver, render_entries
from core.memory.profile_writer import ProfileTableMaintainer

logger = logging.getLogger(__name__)

# optimizer 按 tenant 串行；c18 之后它读写的画像也在同一 tenant 的专表上，
# 这个默认值只服务没传 tenant_id 的调用点（单机与直接构造的测试）。
DEFAULT_OPTIMIZER_TENANT = "default"


class MemoryOptimizerBusy(RuntimeError):
    pass


# ── 提示词 ──────────────────────────────────────────────────────

_MERGE_SYSTEM = (
    "你是一个用户长期记忆整理器。"
    "你的工作不是概括对话，而是从记忆中剔除噪音，"
    "只保留对未来每次对话都产生底色影响的长期记忆。"
)

_MERGE_PROMPT = """\
今日日期：{today}

你的任务是将「现有用户档案」重新整理为一份精炼的长期记忆，同时合并「待合并事实」中的新内容。
但更重要的是：**你必须剔除那些不应该存在于用户档案中的内容。**

## 核心判断标准：缺席成本测试

对每一条内容，问自己：
> 在 6 个月后的一次全新对话中，如果这条信息没有被注入，agent 是否会在某个回复中出现方向性失误？

是 → 保留。否 → 删除。

## 三种应保留的内容

- 「用户事实」：关于用户稳定身份的信息——他是谁、他有什么、他身上不可改变或长期稳定的事实；**当前正在进行的社会角色（就读学校/专业、实习公司+部门+岗位、在职单位+职位）也属于用户事实，必须保留具体细节和现在时态，不得转化为过去时或抽象化**
- 「用户偏好」：用户在对话中持续的审美取向、交互禁忌和根本价值判断——不是具体的爱好列表，而是定义了他是什么样的人的方向性偏好
- 「用户明确要求长期记住的关键内容」：用户亲口说"记住""写进长期记忆"的内容，保持原文连贯性，不删减

待合并事实来自 PENDING.md，采用带 tag 的 bullet 格式：
- [identity] ...
- [preference] ...
- [key_info] ...
- [health_long_term] ...
- [requested_memory] ...
- [correction] ...

tag 含义（与 consolidation 阶段一致）：
- identity：基础信息、稳定背景、长期技术方向、经历、长期设备、长期维护项目
- preference：稳定偏好、禁忌、审美、游戏口味、价值取向
- key_info：允许长期保存的 key / token / id / 账号信息
- health_long_term：长期健康状态的一阶事实，不展开动态指标
- requested_memory：用户明确要求长期记住的关键内容；允许比普通事实更连贯、更完整
- correction：对已有 MEMORY.md 内容的显式修正
- agent_context：助手操作用户环境所需的工具性配置，如服务端口、环境变量名、工具分工、常用登录站点；具体参数（端口号、变量名）必须完整保留，不得抽象化或删除

## 什么必须剔除

### 网络运维细节
内网 IP、路由模式（如"CGNAT""桥接模式""NAT"）、运营商名称、MAC 地址等网络层配置。
→ 这些是瞬时运维信息，不是用户画像。项目路径、配置文件名、环境变量名等与用户开发环境直接相关的信息可以保留。

### 时效性数字和瞬时情绪
具体数字的动态指标（如 Star 数、增长率）、版本变更叙事（"V4 发布后切换"）、瞬时情绪（"失落""失望"）。
→ 保留背后的价值观（如"高度认可某模型"），删除数字和事件过程。

### 临时状态描述
描述当前正在进行、随时会结束的状态（如"最近加班频繁""这周在赶项目""目前在等 offer"）。
→ 与规律性习惯区分：每周/每天持续的行为模式（如"每周去健身房""喜欢手冲咖啡"）可以保留；带"最近""这周""目前"等时间限定词的瞬时状态必须删除。
→ **例外：就读、实习、在职三类社会角色不在此限**，它们定义用户当前的身份位置，应完整保留机构、部门、岗位名称（参见"用户事实"中的保留规则）。删除的是角色内部发生的活动描述，而不是角色本身。

### Agent 执行规则伪装成用户偏好
以"偏好"开头但实际描述 agent 应如何执行（如检索维度划分策略、元数据标注规范等）。
→ 这些是 procedure，不是用户身份，删除。


## 示例

<example id="drop_network_ops_not_project">
输入：
- 开发环境：项目位于 /home/user/project，配置文件 config.toml，需要设置 DB_PASSWORD
- 家庭网络：电信宽带，桥接模式，内网 IP 192.168.1.x

→ 保留开发环境信息（项目路径和配置是用户的开发画像），删除家庭网络（内网 IP 和路由模式是瞬时运维细节，对理解用户无贡献）。
</example>

<example id="drop_timed_metrics">
输入：
- GitHub 仓库截至 3 月已有 200 Star；用户对增长放缓感到焦虑

→ 全部删除。Star 数会过期，情绪是瞬时的——三个月后这条就是谎言。
</example>

<example id="drop_transient_keep_habit">
输入：
- 用户每周去健身房，主要做力量训练
- 用户最近加班频繁，每天靠咖啡撑着

→ 保留"每周去健身房"（规律性习惯，是用户的长期生活方式）。删除"最近加班频繁，靠咖啡撑着"（带"最近"时间限定词的瞬时状态，随时会变）。
</example>

<example id="drop_meta_rule">
输入：
- 信息检索偏好：偏好按多维度分类搜索，能用标签组合过滤，拒绝单一关键词匹配

→ 全部删除。语义上描述的是 agent 应怎么执行检索任务（procedure），不是用户自身的身份或偏好。即使以"偏好"开头，只要主题是系统运作方式，就不属于用户画像。
</example>


## 整理原则
- 合并同类、上收方向**只适用于偏好类内容**：把多条表达同一方向偏好的内容合并为一条方向性陈述，把多个具体知识点上收为一个审美方向
- **身份事实（机构名称、部门、具体岗位、学校/专业）不做上收**：同一机构的多条描述可合并为一条，但不得丢失具体信息，不得抽象化
- 同类重复只保留最终版本
- correction 要直接反映到最终内容中，不要保留"旧值 → 新值"痕迹
- 不要生成 agent 执行规则、SOP、工具调用规范
- 不要保留短期状态、时效性事件
- 普通事实保持简洁；requested_memory 允许保留更完整的连贯描述

## agent_context 特殊规则
- agent_context 条目**必须完整保留**，包括端口号、变量名、URL 等具体参数；不得以"助手可访问某服务"之类的模糊描述替代
- agent_context 内容归入第四节 `## 助手操作上下文`，不与用户事实混合

## 输出格式
- 标题 `# 用户长期记忆`
- 四个大分类：`## 用户事实`、`## 用户偏好`、`## 用户明确要求长期记住的关键内容`、`## 助手操作上下文`
- 每个分类内用 bullet 列表，每条 1-2 行
- `## 助手操作上下文` 若无内容则省略该节
- 直接输出完整档案，不要 JSON，不要代码块，不要任何解释

---

现有用户档案：
{memory}

待合并事实（若有新内容则合并进去，若为空则忽略）：
{pending}
"""

_SELF_SYSTEM = (
    "你是 Rachael，只能更新 SELF.md 中现有的三个 section，不得新增其他 section。"
)

_SELF_PROMPT = """\
你的任务是根据当前 SELF.md 和本轮待合并事实，整理一份新的 SELF.md。

## 目标
- 只输出完整的 SELF.md
- 只允许保留以下三个 section：
  - `## 人格与形象`
  - `## 我对当前用户的理解`
  - `## 我们关系的定义`
- 绝对禁止新增任何其他 section，尤其禁止出现 `## 关系演进记录`

## 更新原则
- 当前 SELF.md 是主文本，优先保留其已有的自我认知、语气和关系定义；不要把待合并事实机械改写进 SELF
- 待合并事实只是辅助证据，只能在它们确实帮助澄清以下内容时少量吸收：
  - Rachael 的定位、说话风格、交互边界
  - Rachael 对当前用户的稳定理解
  - Rachael 与当前用户关系的长期定义
- 大多数待合并事实其实与 SELF.md 无关；无关时直接忽略，不要为了“有输入”而强行改写
- 尤其不要把以下内容写进 SELF.md：
  - 用户资料清单、账号、key、设备参数
  - 健康状态、动态指标、短期计划、近期事件
  - 工具规范、SOP、调用规则、执行流程
  - 对话事件复盘、事件流水账、阶段性经历总结
- 如果没有足够高价值的新信息，宁可输出与当前 SELF.md 基本一致的版本
- 保持语气稳定、简洁、有立场；它是自我认知，不是用户档案，也不是工作日志

## 输出约束
- 输出必须以 `# Rachael 的自我认知` 开头
- 只能包含标题和 bullet 列表
- 不要代码块，不要解释，不要额外说明

---

当前 SELF.md：
{self_content}

待合并事实：
{pending}
"""

# ── MemoryOptimizer ───────────────────────────────────────────────


class MemoryOptimizer:
    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        *,
        # c18 ADR-3/ADR-14（task 6.11）：pending 的读与「删除 + 写回」都按租户走专表；
        # 归属为空或解析失败就整轮跳过（task 4.3 fail-closed）。画像层已没有文件后端，
        # 所以这是 optimizer 唯一的画像入口（task 6.10）。
        profile_stores: ProfileStoreResolver,
        max_tokens: int = 16384,
        default_self_md: str = "",
        identity_name: str = "",
        # C8 §5.9.16：optimizer work-start snapshot lease。
        runtime_snapshot_store: "RuntimeSnapshotStore | None" = None,
        # c9-persona-relationship ADR-5 / c18 ADR-10：RelationshipState 的读写缝，
        # 两种后端各一个实现；None 时 SELF 这一路跳过（见 `_update_self`）。
        relationship_io: "Any | None" = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._max_tokens = max_tokens
        # C3 §5.9.5/§10 DECIDED：optimizer lock 按 tenant 隔离，不引入跨 tenant
        # global maintenance lock；同一租户内串行，不同租户互不阻塞。
        self._runtime_snapshot_store = runtime_snapshot_store
        self._relationship_io = relationship_io
        self._profile_stores = profile_stores
        self._locks: dict[str, asyncio.Lock] = {}
        self._default_self_md = default_self_md
        self._identity_name = identity_name

    # 各步骤之间的间隔（秒），避免短时间内连续请求触发 limit_burst_rate
    _STEP_DELAY_SECONDS: int = 15

    def _lock_for(self, tenant_id: str) -> asyncio.Lock:
        lock = self._locks.get(tenant_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[tenant_id] = lock
        return lock

    @property
    def is_running(self) -> bool:
        """任一 tenant 的 optimizer 在运行（Dashboard 手动优化按钮观测口径不变）。"""
        return any(lock.locked() for lock in self._locks.values())

    async def optimize(self, tenant_id: str = DEFAULT_OPTIMIZER_TENANT) -> None:
        """两步优化：合并 PENDING → MEMORY，更新 SELF（按 tenant 串行）。"""
        lock = self._lock_for(tenant_id)
        if lock.locked():
            raise MemoryOptimizerBusy("memory optimizer 正在运行")
        from agent.plugins.snapshot import work_runtime_lease

        # C8 §5.9.16：optimizer work start 取一次 snapshot lease。
        async with work_runtime_lease(self._runtime_snapshot_store):
            async with lock:
                await self._optimize(tenant_id)

    def _profile_for(self, tenant_id: str) -> ProfileTableMaintainer | None:
        """本轮优化用哪个租户的画像表；返回 None 表示什么都不做（task 4.3 fail-closed）。

        三条判据按顺序：装配根没给解析器、归属为空、解析器报错——都只跳过，并留一条带
        当时归属的日志。不选「回退到进程共享的文件目录」：那等于把这次优化写进所有租户
        共用的一份画像里，正是本 change 要关的入口。
        """
        resolver = self._profile_stores
        if resolver is None:
            logger.error(
                "c18 fail-closed：optimizer 没有画像存储解析器，跳过本轮优化"
            )
            return None
        if not str(tenant_id or "").strip():
            logger.error(
                "c18 fail-closed：optimizer 拿不到可信租户归属，跳过本轮优化"
            )
            return None
        try:
            store = resolver(tenant_id)
        except Exception:
            logger.exception(
                "c18 fail-closed：画像存储解析失败，跳过本轮优化（tenant_id=%r）",
                tenant_id,
            )
            return None
        return ProfileTableMaintainer(store, tenant_id)

    async def _optimize(self, tenant_id: str = DEFAULT_OPTIMIZER_TENANT) -> None:
        """把该租户的 pending 合进 long_term，随后更新自我认知。"""
        profile = self._profile_for(tenant_id)
        if profile is None:
            return

        # 1. 先读这一轮的输入：pending 行连主键一起留着，写回时按主键点名删除。
        rows = await profile.read_pending_rows()
        pending = render_entries(rows)
        current_memory = (await profile.read_long_term()).strip()
        if not current_memory and not pending:
            logger.info("[memory_optimizer] 记忆和 pending 均为空，跳过优化")
            return

        # 2. 间隔延迟后并行启动 MEMORY 合并和 SELF 更新
        await asyncio.sleep(self._STEP_DELAY_SECONDS)
        merged_memory, _ = await asyncio.gather(
            self._merge_memory(current_memory, pending),
            self._update_self(pending, tenant_id=tenant_id),
        )

        # 3. 合并成功才落笔：删掉这轮吃进去的行 + 覆写 long_term，同一个事务。
        #    合并返回空或抛异常时一条都不删——文件版那套 snapshot/commit/rollback 三步
        #    在这里没有对应物，因为「什么都没做」本身就是可恢复的状态（ADR-14 修订）。
        if not merged_memory:
            logger.warning(
                "[memory_optimizer] 合并返回空，保留原有内容，%d 行 pending 原样留下",
                len(rows),
            )
            return
        deleted = await profile.merge_pending_into_long_term(
            [row.id for row in rows], merged_memory
        )
        logger.info(
            "[memory_optimizer] 记忆已合并 before=%d after=%d chars，取走 %d 行 pending",
            len(current_memory),
            len(merged_memory),
            deleted,
        )

    async def _merge_memory(self, memory: str, pending: str) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        prompt = _MERGE_PROMPT.format(
            today=today,
            memory=memory or "（空）",
            pending=pending or "（无新内容）",
        )
        return await self._request_text_response(
            system_content=_MERGE_SYSTEM,
            user_content=prompt,
            max_tokens=self._max_tokens,
        )

    async def _update_self(self, pending: str, *, tenant_id: str = DEFAULT_OPTIMIZER_TENANT) -> None:
        """只更新自我认知现有的三段，不新增 section。

        读写走 persona 那条缝（ADR-9/ADR-10：单事务 + 审计，两种后端各一个实现）。
        缝为 None 时跳过并留记录——这条路径今天只在「PG 后端但没开 durable webchat」时出现，
        那个装配缺口的成因与影响写在 c18 的 tasks 4.2 注记里，不在这里用共享文件目录兜底。
        """
        if self._relationship_io is None:
            logger.warning(
                "[memory_optimizer] 没有 RelationshipState 写缝，跳过 SELF 更新（tenant_id=%r）",
                tenant_id,
            )
            return
        self_content = (
            await self._relationship_io.read(tenant_id)
        ).strip() or DEFAULT_SELF_MD.strip()
        if not self_content:
            logger.info("[memory_optimizer] SELF 不存在或为空，跳过更新")
            return
        prompt = _SELF_PROMPT.format(
            self_content=self_content,
            pending=pending or "（无新内容）",
        )
        updated = await self._request_text_response(
            system_content=_SELF_SYSTEM,
            user_content=prompt,
            max_tokens=2048,
        )
        if updated:
            await self._relationship_io.write(tenant_id, updated)
            logger.info("[memory_optimizer] SELF 已更新（RelationshipState 写缝）")

    async def _request_text_response(
        self,
        *,
        system_content: str,
        user_content: str,
        max_tokens: int,
    ) -> str:
        resp = await self._provider.chat(
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            tools=[],
            model=self._model,
            max_tokens=max_tokens,
        )
        return (resp.content or "").strip()


# ── MemoryOptimizerLoop ───────────────────────────────────────────

_DEFAULT_INTERVAL_SECONDS = 64800  # 默认每 18 小时整点


class MemoryOptimizerLoop:
    def __init__(
        self,
        optimizer: MemoryOptimizer | None,
        interval_seconds: int = _DEFAULT_INTERVAL_SECONDS,
        _now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._optimizer = optimizer
        self._interval = max(60, interval_seconds)
        self._now_fn = _now_fn or datetime.now
        self._running = False

    async def run(self) -> None:
        self._running = True
        logger.info(
            "[memory_optimizer] 优化循环已启动，间隔=%ds (%.1fh)，对齐整点",
            self._interval,
            self._interval / 3600,
        )
        while self._running:
            secs = self._seconds_until_next_tick()
            logger.info(
                "[memory_optimizer] 距下次优化 %.0f 秒 (%.1f 小时)",
                secs,
                secs / 3600,
            )
            await asyncio.sleep(secs)
            if not self._running:
                break
            try:
                if self._optimizer:
                    await self._optimizer.optimize()
            except Exception:
                logger.exception("[memory_optimizer] 优化异常")

    def stop(self) -> None:
        self._running = False

    def _seconds_until_next_tick(self) -> float:
        """计算距下一个对齐整点的秒数。"""
        now = self._now_fn()
        now_ts = now.replace(second=0, microsecond=0).timestamp()
        next_ts = (now_ts // self._interval + 1) * self._interval
        return max(1.0, next_ts - now.timestamp())
