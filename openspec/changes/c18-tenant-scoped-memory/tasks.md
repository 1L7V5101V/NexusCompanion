## 1. 分支与基线

- [ ] 1.1 从 `main` 开 `feature/c18-tenant-scoped-memory`（独立 worktree，避免与在跑的本地后端互扰）；跑基线门禁 `.venv/Scripts/pyright.exe --level error -p pyrightconfig.json`、`-p pyrightconfig.tests.json`、`.venv/Scripts/python.exe -m pytest -q -W error tests/`，三份输出存 `openspec/evidence/c18-tenant-scoped-memory/baseline.txt`。完成判据：基线错误数与失败用例数被记录为后续对照点
- [ ] 1.2 确认 `agent/memory_pg.py::PgMemoryMarkdownStore` 仍无实例化点（`grep -rn "PgMemoryMarkdownStore" --include=*.py . | grep -v memory_pg.py` 只应剩 `persona_repo.py` 的注释）；结论记入 evidence 目录 `pre-wiring-check.txt`

## 2. 替身与失败测试先行

- [ ] 2.1 改 `tests/memory_fakes.py:25`：替身改为可注入的画像快照提供者，保留 `MemoryStore(workspace)` 构造签名给单机用例。验证：`pytest -q -W error tests/memory_fakes.py tests/test_memory_store_questions.py` 通过
- [ ] 2.2 新增静态扫描测试 `tests/memory/test_no_shared_profile_io.py`，落实 spec「运行时路径无共享目录读写」：扫描 `core/memory/runtime.py`、`bootstrap/memory.py`、`bootstrap/tools.py`、`bootstrap/app.py`、`bootstrap/proactive.py`、`plugins/drift_flow/`、`agent/core/prompt_block.py`、`memory2/store.py`，断言不存在指向共享 `workspace/memory` 的读取分支与「查不到就改读共享目录」的兜底分支。验证：本任务结束时该测试为红（预期红，作为后续实现的靶）
- [ ] 2.3 新增 `tests/memory/test_profile_snapshot_isolation.py`，落实 spec「新租户开场白不引用他人话题」与「画像缺失不回退共享位置」两个 Scenario：构造共享目录留有他租户摘要的场景，断言组装结果零出现。验证：红

## 3. 画像快照与 prompt 组装

- [ ] 3.1 定义 `MemoryProfileSnapshot`（五类字段的不可变对象），放在 `agent/core/types.py` 与 `PersonaSnapshot` 并列；不扩 `PersonaSnapshot`（design ADR-2）。验证：`pyright --level error -p pyrightconfig.json` 无新增错误
- [ ] 3.2 实现 turn 入口的批量预取：单条查询取回五类，在租户 lane 内解析一次并随 ContextRequest 传递（design ADR-1）。验证：`tests/memory/test_profile_prefetch.py` 新增用例断言一轮只发一次画像查询（以 SQL 计数或 fake session 调用次数计）
- [ ] 3.3 `TurnContext` 补 `tenant_id`（`agent/core/prompt_block.py:24-33`），`agent/context.py:335-346` 组装时透传。验证：`tests/persona/test_prompt_blocks.py` 全部用例通过，且新增断言 `tenant_id` 缺失时构造失败而非默认租户兜底
- [ ] 3.4 两个画像块（`prompt_block.py:185` memory、`:216` recent_context）改读快照。验证：2.2 与 2.3 两个测试转绿
- [ ] 3.5 补 `_append_entry`（`agent/memory_pg.py:240-258`）不写 `happened_at` 的缺口：journal / history 写入时事件日期落列，`summary` 去掉日期前缀（design ADR-8）。验证：`tests/memory/test_journal_row_shape.py` 断言写入一行 journal 后 `happened_at` 非空且 `summary` 不含 `**` 日期前缀，通过

## 4. 代理清理与启动期路由

- [ ] 4.1 删除 `core/memory/runtime.py:36-49` 四个无租户代理方法，改为显式收 `tenant_id` 的解析入口（design ADR-3，不用 contextvar）。验证：`grep -n "def read_self\|def read_long_term\|def get_memory_context" core/memory/runtime.py` 显示签名含租户参数；pyright 无未调用方残留错误
- [ ] 4.2 改造六处启动期引用点为「持有解析器、处理某租户数据时才解析」：`bootstrap/app.py:666`（optimizer）、`:738`（dashboard）、`:775`（proactive）、`bootstrap/tools.py:579`（ContextBuilder）、`:597`（maintenance 绑会话生命周期）、`plugins/drift_flow/factory.py:114`（drift 经 `scope.memory`）（design ADR-4）。验证：`pytest -q -W error tests/test_runtime_smoke.py tests/test_memory_optimizer.py tests/test_memory_engine_contract.py` 通过
- [ ] 4.3 后台写入 fail-closed：整理/压缩/消费待合并事实时拿不到可信租户归属则跳过并留可定位记录，不落共享目录、不落默认租户。验证：`tests/memory/test_background_write_failclosed.py` 覆盖三条后台路径各一例，通过

## 5. 公共租户目录 helper

- [ ] 5.1 把 `agent/tools/path_resolver.py:58-68` 的私有 `_category_root` 逻辑抽成公开 `tenant_category_root(workspace_root, tenant_id, category)`，`bootstrap/attachments/blob_store.py:71-85` 改用它并删除复制实现（design ADR-5）。验证：`pytest -q -W error tests/ -k "blob or path_resolver or attachment"` 通过，且 `grep -rn 'tenants.*tenant_dirname' --include=*.py` 只剩 helper 一处

## 6. 单机侧表存储与自我认知归属收敛

- [ ] 6.1 `memory2/store.py` 的建表语句加 `tenant_persona_profiles` 与 `persona_audit_events` 两张（`persona_templates` 不建，单机无模板面），列集合与 CHECK 对齐 PG 侧 `bootstrap/db/models/persona.py`；`source` 复用 `('template','custom')`，不改 PG 约束、alembic 零迁移（design ADR-10）。验证：`tests/memory/test_persona_schema_parity.py` 比对两种后端 persona 表的列名集合与 CHECK 取值，通过
- [ ] 6.2 workspace 初始化时为单机固定租户播种一行 `tenant_persona_profiles`（`identity` / `personality_rules` / `self_model` 取 `[agent.persona]`，`source='custom'`），仅在行不存在时写入。验证：`tests/bootstrap/test_single_user_persona_seed.py` 断言首次初始化建行、二次初始化不覆盖已演化的当前值，通过
- [ ] 6.3 关系状态写缝加 SQLite 实现，与 `PgRelationshipIO` 同语义（当前值 upsert + 同事务审计行）；`MemoryOptimizer` 两种后端都只经该缝写自我认知，画像层的 `read_self` / `write_self`（`agent/memory_pg.py:135-139`）删除。验证：`tests/memory/test_self_writer_parity.py` 断言两种后端各写入一次后当前值为后一次完整结果、且各留一条同形态审计事件，通过
- [ ] 6.4 新增 SQLite 侧画像存储实现，接口与 `PgMemoryMarkdownStore` 对齐，落在 `memory2/store.py:69-82` 已有的 `memory_items` 表上、归属 `infra/storage/tenancy.py:22` 的 `DEFAULT_TENANT`（design ADR-10）。验证：`tests/memory/test_sqlite_profile_store.py` 覆盖五类读写各一例，通过
- [ ] 6.5 放宽 `bootstrap/app.py:649` 的装配条件，让两种后端都装 persona resolver 与写缝（现仅 PG durable 装配，单机恒无快照）。验证：`tests/memory/test_single_user_persona_snapshot.py` 断言单机装配下 `persona_snapshot` 非 None 且三字段来自播种行，通过
- [ ] 6.6 `SelfModelPromptBlock` 删掉 else 分支，只读 `persona_snapshot.relationship_state`；快照为 None 时跳过注入，SHALL NOT 回退读共享文件（design ADR-9）。验证：`tests/persona/test_prompt_blocks.py` 原两条分支用例合并为一条来源断言，另加一条 None 不回退的用例，通过
- [ ] 6.7 摘掉 markdown 文件后端的装配：`bootstrap/memory.py:170` 与 `:246` 两处不再产出文件 store，`agent/memory.py` 的 `MemoryStore` 退出运行时路径。验证：2.2 的静态扫描测试转绿；`grep -rn "MemoryStore(" --include=*.py bootstrap/ agent/ core/` 无运行时装配命中

## 7. 画像迁移脚本

- [ ] 7.1 新增 `scripts/migrate_profile_to_tenant.py`：必须显式 `--tenant`，缺参 fail-closed 退出非零；目标后端按配置解析（PG 走租户、SQLite 走固定租户），不假设只有 PG——废弃文件后端后，单机现存的 markdown 画像同样要迁进 `memory2` 的表。默认 dry-run 打印目标租户五类当前字符数、来源文件字节数，以及每类正文首行摘要；`SELF.md` 不在画像迁移范围内，它的内容交给 6.2 的播种路径（已有播种行则不覆盖），脚本只报告不搬。显式执行才写入（design ADR-6、ADR-9、spec「未指定归属时拒绝」）。验证：不带 `--tenant` 执行返回非零且目标库与共享目录零变化；两种后端各跑一次 dry-run 均输出可读的字节数与正文摘要对照
- [ ] 7.2 实装迁移，五类语义不合并；所有需幂等的写入一律走 `_append_once_with_index`，**禁止**用 `_append_entry`——它的 `content_hash` 是 `str(hash(...))`，跨进程随机化且全仓库未固定 `PYTHONHASHSEED`，靠唯一索引去重在重放时必然失效（design Risks）。journal 按 `journal/YYYY-MM-DD.md` 逐行迁入，文件名日期写入 `happened_at`，`summary` 不再拼 `**YYYY-MM-DD** ` 前缀（design ADR-8）。验证：同一来源连跑两次，第二次后 `memory_items` 中该租户条目数不变（spec「重放不产生重复条目」）；抽查 journal 行的 `happened_at` 日期集合与原目录文件日期集合一一对应；输出存 `openspec/evidence/c18-tenant-scoped-memory/migration-replay.txt`
- [ ] 7.3 迁移后原文件保持原样。验证：执行前后对共享目录六文件做 sha256 比对一致，结果并入同一 evidence 文件

## 8. 本地验收工具

- [ ] 8.1 建两个常驻测试租户，并在测试台清单里记 label → `tenant_id` 映射（design ADR-7：不改 `_new_tenant_id()`，`admin1` / `admin2` 写进 `display_name`）。验证：两账号 `display_name` 分别为 admin1 / admin2 且状态 active、各自租户在 `tenant_persona_profiles` 有行；重启后端后按 `display_name` 仍能解析出同一对 `tenant_id`，映射表存 `openspec/evidence/c18-tenant-scoped-memory/tenant-labels.txt`
- [ ] 8.2 dev-only 租户切换入口，且加断言证明生产配置下该路由不注册（判据见 design ADR-6：账号与租户 1:1 由 `uq_canonical_conversations_tenant_id` 固定）。验证：`tests/auth/test_tenant_switch_dev_only.py` 断言非 dev 配置下入口返回 404，通过
- [ ] 8.3 跨租户隔离实机断言，**必须在 PG 后端上跑**：SQLite 的 `memory_items` 无 `tenant_id` 列，单机"隔离"只等于"只有一个租户"，验不出东西。做法是 admin1 写入一条可识别的画像内容，切到 admin2 组装一轮，断言其系统提示与回复中零出现该片段。验证：手工执行记录 + 自动化用例各一份，输出存 `openspec/evidence/c18-tenant-scoped-memory/isolation-admin1-admin2.txt`

## 9. 门禁与状态收口

- [ ] 9.1 同步既有断言清单（proposal.md — Impact 列出的 9 处），使全量测试通过。验证：`pytest -q -W error tests/` 零失败，输出存 `openspec/evidence/c18-tenant-scoped-memory/pytest-full.txt`
- [ ] 9.2 类型门禁。验证：`pyright --level error` 两配置输出存 `openspec/evidence/c18-tenant-scoped-memory/pyright.txt`，错误数不高于 1.1 基线
- [ ] 9.3 `openspec validate c18-tenant-scoped-memory --strict` 通过；`openspec status --change c18-tenant-scoped-memory` 四份 artifact 均 done
- [ ] 9.4 仅在有 evidence 的前提下更新 `openspec/SCALING_ROADMAP.md` 与 `openspec/PILOT_ROADMAP_PROJECT_CHECKLIST.md` 中记忆画像隔离相关条目状态；无证据的条目不动
- [ ] 9.5 回填 `openspec/specs/tenant-memory-profile/spec.md`（archive 时生成主 spec），并在 `PILOT_ROADMAP.md` 的实现地图接缝处标注该 capability 已建立

