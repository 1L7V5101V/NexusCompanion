# C18 修复计划（按优先级）

配套文档：同目录 `architecture-review.md`（发现编号 F1–F13 的定义与证据在那里，本文不重复）。
关系图：同目录 `risk-map.dot`。

负责人一栏是**假设**，本仓库目前看是单人推进，写出来只是为了标明「谁该做决定」——决策类和实现类要分开。

**状态（2026-10-12）：P0 三条决策已定案并落进计划，本文的 P0 段落是决策前的快照，未改写。** D1 选了「按新表重设接口」，落 ADR-14；D2 选了选项 3「另立专表」，落 ADR-13，并按行形状分成两张；D3 的前提「三条旁路经 `ContextBuilder` 组装」经查证不成立，改为三条旁路共用一个提公开的段渲染器，落 ADR-15。逐条发现的状态见同目录 `architecture-review.md` 开头的状态表。下文 P1–P3 的条目凡已被 ADR-13 / 14 / 15 消解的，以那份状态表为准。

---

## 优先级判据

排序按四个维度综合：影响面（炸了会波及多少租户）、可逆性（数据丢了能不能找回）、是否阻断后续任务、修复成本。

**静默的数据丢失排在最前**，因为它的故障表现是「记忆没有了」而不是报错，发现时通常已经不可逆。

---

## P0 先做决定，不要先写代码

这三条会改 design.md 的结论。在它们定案之前继续往下实现，等于把返工排进队列。

### D1 `PgMemoryMarkdownStore` 是补还是重写（对应 F1、F3、F8、F9）

- **决策人**：变更作者。
- **要回答的问题**：补上 pending 消费语义、修掉去重注册表的两个缺陷、把六处哈希换成稳定实现、给单行类型加唯一约束——这四件事加起来，是否已经比重写一个画像存储实现更贵？
- **判据**：把四项各自要动的行数估出来，与「按 `MemoryProfileApi` 的方法集重写一个 PG 实现」对比。重写的好处是可以按表存储的真实约束设计，而不是照着文件版的语义翻译。
- **倾向**：F1 的空实现不是笔误，它说明这个类当初是照着文件版的接口形状填的，没有对着调用方的真实语义填。这类实现补齐之后仍然会留下「接口对齐但语义不对齐」的隐患。倾向重写，但要作者确认。
- **验收**：design.md 里新增一条 ADR，写明选择与理由；被否掉的那个选项要写出代价。

### D2 画像层住哪张表（对应 F4、F8、F11）

- **决策人**：变更作者。
- **三个选项**：
  1. 继续共用 `memory_items`，在召回侧加默认类型排除集。代价：动召回代码，proposal 的 Non-Goal「不动向量/BM25 检索面」要改写；`memory-retrieval-baseline` 与 `memory-engine-catalog` 两份主 spec 是否受影响要重新判定。
  2. 继续共用 `memory_items`，画像内容不写进参与 BM25 索引的 `summary` 列。代价：索引与查询都要改，且「同一张表两套列语义」比现在的状态更难懂。
  3. 画像层另立一张表。代价：推翻 proposal.md:46 的「PG 侧无新表、alembic 零迁移」，要加一次迁移；好处是画像与召回彻底解耦，F4 与 F8 一起消失，`memory_items` 的语义回到「可召回的记忆条目」。
- **判据**：选项 3 的迁移成本，对比选项 1 需要重新跑召回评测并可能重设基线的成本。后者依赖 F11 先修好评测工具。
- **倾向**：选项 3。理由是它把「画像」与「可召回记忆」这两个本来就不是一回事的概念分开，而选项 1 与 2 都是让一张表承担两种语义、再靠过滤器把它们隔开。
- **验收**：design.md 新增 ADR；proposal.md 的 Impact 与 Non-Goals 同步改写；若选 3，补 alembic 迁移的任务。

### D3 三条旁路各自的形状（对应 F2、F6）

- **决策人**：变更作者。
- **要表态的路径**：主动推送（`plugins/proactive_flow/prompt.py`）、drift（`plugins/drift_flow/runtime.py`）、唤醒推送（`plugins/wake_proactive/runtime.py`）。
- **每条路径二选一**：消费 ADR-1 的预取快照，或者改成 `await` 调用。选之前要先确认这三条路径是否经 `ContextBuilder` 组装——如果不经，快照就传不进去，只能选后者。
- **连带项**：提示词模板 `prompts/agent.py:38-41,152` 与 `prompts/background.py:32-36,112-113` 里的画像文件路径要一并摘掉。宏观时间线浏览那条指示改成走召回工具，具体改成什么取决于 D2。
- **验收**：design.md 的 ADR-4 扩写成六处加三处；proposal.md 的 Impact 补 `plugins/proactive_flow/`、`plugins/wake_proactive/`、`prompts/`；tasks 新增对应条目。

---

## P1 静默数据丢失类

### R1 修 pending 消费语义（F1）

- **前置**：D1 定案。
- **动作**：让 PG 侧的「冻结待合并内容」有真实的消费效果，失败时能回滚。SQLite 侧同语义。
- **验收**：一条端到端用例，PG 后端上跑：塞两条待合并事实 → 跑一次 `_optimize` → 断言长期画像含这两条内容、且 `memory_type='pending'` 的行数为 0。再加一条失败路径用例：让合并返回空，断言待合并行仍在。两种后端各跑一遍。

### R2 修去重注册表（F3）

- **前置**：D1 定案（若重写，这条并入重写范围）。
- **动作**：去重键改成短且租户内唯一的形状，`tenant_id` 显式参与哈希原文。是否给 `app_configs` 主键加 `tenant_id` 要单独表态——加了就需要迁移，与「alembic 零迁移」冲突。
- **验收**：两条用例。一条断言含 5 条消息的整理窗口写入 journal 不报错且 key 长度在列上限内；一条断言两个租户用同一 `source_ref` 各写一次后两边都有行。

### R3 存量单机的自我认知要导入，不是重新播种（F7）

- **动作**：任务 6.2 的播种输入改成「磁盘 `SELF.md` 存在且非出厂默认则取文件内容，否则取 `[agent.persona]`」；任务 7.1 从「只报告」改成「导入并把原文件重命名留档」。任务 6.7 补上 `bootstrap/init_workspace.py`：停止创建四个画像 markdown，停止用 `MemoryStore(workspace)` 建 `consolidation_writes.db`。
- **验收**：一条用例，准备一个 `SELF.md` 已被改写过的 workspace，跑升级流程，断言表里那行的 `self_model` 等于文件内容而不是 config 默认值。另一条断言升级后共享目录下不再新建画像文件。

---

## P2 护栏与覆盖面

### R4 静态扫描改成全仓库加豁免清单（F5）

- **动作**：`tests/memory/test_no_shared_profile_io.py` 的扫描范围从 7 文件加 1 目录，改成全仓库 `*.py` 减去一份显式豁免清单（`tests/`、`eval/`、`docker/debug/`、`scripts/`）。清单里每一项写明豁免理由。补两条正则：一条覆盖把 `memory_runtime` 门面整体当实参传递的形态，一条覆盖 `getattr(..., "maintenance")` 这类字符串取用。
- **验收**：改完之后测试应当逮到评审列出的十处漏项（`agent/looping/core.py` 四处、`bootstrap/dashboard_api.py:957`、`bootstrap/init_workspace.py:150`、`bootstrap/app.py:775`、`proactive_v2/memory_optimizer.py`、`plugins/proactive_flow/`、`plugins/wake_proactive/`、`infra/channels/qq_channel.py:692`、`agent/config.py:807`）。逮不到就说明规则还不够。

### R5 补齐 5 个无任务的 Scenario（F10）

- **动作**：给「并发双租户写不互串」「新租户画像从空开始」「三条组装路径同源」「单机模式写入落表不落文件」「双租户各自整理」各立一个任务，任务描述里点名它落实哪个 Scenario。persona 那两条重启恢复场景合并成一个任务，覆盖两种后端。
- **验收**：tasks.md 里每个 Scenario 都能反查到至少一个任务；归档前跑一遍 architecture-review.md 里 F10 那张对照表。

### R6 离线工具表态（F11）

- **动作**：proposal.md 的 Impact 加一段离线工具清单，逐个写「修／暂留坏／删」。至少要保住 D2 验证需要的那条召回评测路径（`eval/locomo` 或 `eval/longmemeval`）。
- **验收**：清单里每一项有明确处置；被标「修」的有对应任务。

### R11 统一「单机模式」的判据（F13）

- **动作**：两步。第一步改 spec 措辞——`specs/tenant-memory-profile/spec.md` 里「单机模式（存储后端为 SQLite 或认证关闭）」这句，要么把「或」改成与 `bootstrap/tools.py:457-463` 一致的「且」，要么写明两种判据不同以及理由。第二步加启动期校验：`auth.enabled=true` 时要求 `storage.backend="postgres"`，不满足则拒绝启动，报错文本照 `bootstrap/telegram_durable.py:53-70` 的形状写。
- **时机**：第一步属于 delta spec 的措辞修订，跟着 D1–D3 那次 `openspec-update-change` 一起做，不要单开一趟。
- **验收**：一条用例断言 `auth.enabled=true` + `storage.backend="sqlite"` 启动失败且报错文本点明两个配置项；spec 里的判据与 `multi_tenant` 的算式逐字对得上。

---

## P3 质量收尾

### R7 哈希收敛（F9）

- **动作**：六处 `str(hash(...))` 全改成 `memory2/store.py:153-155` 的 `_content_hash`。这是一次收敛，不是新增机制。
- **验收**：`grep -rn "str(hash(" --include=*.py` 在运行时路径下零命中；一条用例断言同一内容在两个进程里算出的 `content_hash` 相同（用 subprocess 起两个解释器，不固定 `PYTHONHASHSEED`）。

### R8 同租户并发唯一性（F8）

- **前置**：D2 定案（若画像另立表，约束设计跟着变）。
- **动作**：给单行类型加唯一约束，或把 `_upsert_content` 改成 `INSERT ... ON CONFLICT DO UPDATE`。
- **验收**：一条用例，同一租户并发两次 `write_long_term`，断言表里该类型只有一行。

### R9 影响清单补全（F12）

- **动作**：proposal.md:48 的测试清单从 9 处补到实际的 15 处以上。
- **验收**：任务 9.1 跑全量 pytest 零失败即可兜住，这条只为估算准确。

### R10 顺手核对一处不一致

- **动作**：`bootstrap/app.py:989` 的 `AttachmentBlobStore(..., multi_tenant=True)` 是硬写 True，而 `bootstrap/tools.py:459` 与 `:730` 按 `auth.enabled ∧ backend=postgres` 算。ADR-5 要动 blob_store，顺便确认这处是有意还是漏改。
- **验收**：确认结论写进 ADR-5，或改掉。

---

## 顺序建议

```
D1 D2 D3（三个决定，可并行讨论，但都要在写代码前定案）
   ↓
走一次 openspec-update-change，把新 ADR、改写后的 Impact / Non-Goals、以及 R11 第一步的 spec 措辞一并落进产物
   ↓
R4（先把护栏范围补全，让后面的实现有靶）
   ↓
R1 R2 R3（静默数据丢失三条，可并行）
   ↓
R5 R6 R11（补场景、离线工具表态、启动期校验）
   ↓
R7 R8 R9 R10（质量收尾）
   ↓
门禁：pyright 两配置 + pytest -q -W error tests/
```

R4 排在实现之前的理由：护栏范围不全的时候继续实现，漏改的旁路不会报警，等到全量测试才发现就要回头翻已经改完的地方。
