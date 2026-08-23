# Phase 1B 迁移工具与主数据源切换 — 实施任务

> 执行环境：独立 branch/worktree（如 `feature/phase1b-migration`），不在主工作树直接实现。
> 每任务含验证方式；管理闭环：任务 checkbox → `openspec status` → evidence → SCALING_ROADMAP/CHECKLIST 仅在有证据时更新。

## 1. 基线与环境

- [ ] 1.1 创建独立 worktree/branch（`feature/phase1b-migration`），记录 main 与分支的 pytest/pyright 基线到 `openspec/evidence/phase1b/baselines/`。验证：`git status --short --branch` 干净、分支正确；基线文件存在且含 main 全量 1031 passed / 0 failed（2026-08-23 清理后）记录
- [ ] 1.2 准备 staging PG（复用 `docker/debug/docker-compose.yml`）+ alembic `upgrade head` + 样例 SQLite 数据（覆盖多 tenant/多通道）。验证：PG 可连、schema 就绪、`import_to_pg.py` 在基线可用（校验工具为本次新建目标，见 §3）

## 2. M5 批量导入（C1B）

- [ ] 2.1 实现批量写入后端（独立 COPY 连接；仅 `memory_items` 需导入前按 tenant 幂等 provisioning，复用 `partition_name_for_tenant` 与 advisory-lock 双检；`sessions`/`messages`/`memory_replacements` 为普通表直接 COPY）。验证：`pytest tests/migration/` 覆盖 COPY 路由到已建 `memory_items` 分区与非分区表；1 万行样例导入行数与源一致且耗时显著低于逐行基线（记录数字到 evidence）
- [ ] 2.2 实现 checkpoint 断点续传（每表主键高水位、JSON 原子写 `<run-id>.json`）。验证：构造「中途中断 → 续跑」用例，最终逐表行数与源一致；checkpoint 文件在中断后正确记录边界
- [ ] 2.3 实现幂等重跑（保留源主键 + `ON CONFLICT DO NOTHING`）。验证：对同一源重跑导入，行数不变、无重复；测试覆盖
- [ ] 2.4 实现显式 tenant mapping 强制（mapping JSON；dry-run 列出未映射源；缺 mapping 拒绝写入）。验证：无 mapping 时工具预检终止且 PG 零写入；mapping 生效后各 tenant 数据跨 tenant 不可见（复用 `test_tenant_isolation` 模式）
- [ ] 2.5 覆盖全部目标表（`memory_items`/`sessions`/`messages`/`memory_replacements`/纳入本阶段的插件数据），按 FK 顺序导入（sessions → messages → memory_items → memory_replacements）。验证：逐表行数与 SQLite 源一致
- [ ] 2.6 import 机器可读结果入库（`openspec/evidence/phase1b/results/`）。验证：结果文件存在、字段完整、可复现（重跑后结果一致）

## 3. M5 机器可读校验（C1B）

- [ ] 3.1 实现逐表行数 + 关键字段 hash 校验（按主键排序后对规范字段做 hash）。验证：构造行数/字段差异样例 → 报告指出差异表与维度、退出码非零
- [ ] 3.2 实现引用完整性校验（`messages.session_key` 命中 sessions、`memory_replacements` 外键命中）。验证：孤儿引用被报告为失败
- [ ] 3.3 实现语义抽样校验（同一查询下源/目标向量 top-k 命中与排序、关键词搜索命中集合、session `next_seq`）。验证：抽样查询源/目标结果一致；允许差异有记录
- [ ] 3.4 校验报告 JSON 输出 + evidence 入库（`openspec/evidence/phase1b/results/`）。验证：报告文件存在、各维度字段完整、退出码语义正确

## 4. M6 S0-S4 主数据源切换（C1C）

- [ ] 4.1 实现状态持久化（JSON 状态文件 + 原子写 + `status` 命令）。验证：状态推进可观察、中断后从持久化状态恢复、不重复已完成步骤
- [ ] 4.2 实现 S1 进入/退出条件 gate（import + verify 证据齐备才允许推进 S2）。验证：校验未通过时 `promote` 被拒并给出缺失证据
- [ ] 4.3 实现 S1→S2 promote（config 切 `backend="postgres"` + staging 全链路 smoke + 对账报告）。验证：staging 全链路可用、新写入以 PG 为准、对账报告入库
- [ ] 4.4 实现 turn control dual-store（PG-primary 下 SessionManager 持 SQLite turns store，`control_store` 不再抛 RuntimeError）。验证：PG-primary smoke 下 agent turn 关键路径可跑、turn 记录落 SQLite 审计副本；切换记录声明该边界与恢复/回滚策略
- [ ] 4.5 实现 S2→S3 audit（完成至少一个业务周期对账、停止 shadow、SQLite 归档只读）。验证：对账报告入库、shadow 停止后 SQLite 只读、S3 证据文件存在
- [ ] 4.6 实现回滚（S1 内回 SQLite primary；S2 起禁止 rollback-to-SQLite）。验证：S1 回滚演练数据完整；S2 后调用回切命令被拒并提示正确口径
- [ ] 4.7 PITR 恢复演练 + runbook。验证：恢复数据与指定时间点一致；runbook 记录恢复/回滚步骤与 RPO≤5min/RTO≤30min

## 5. 收口与证据

- [ ] 5.1 全量验证：pyright（project + tests 两配置）与 pytest 相对 main 基线无回归。验证：命令结果 + 与 `openspec/evidence/phase1b/baselines/` 记录的 main 基线 diff，无本分支新增失败
- [ ] 5.2 更新 change 状态与跟踪文档：tasks 全勾选 → `openspec validate` 通过 → `openspec status` 显示 apply 完成 → sync-specs 把 `storage-migration` 增量合入主 spec → archive change。验证：`openspec validate` 通过、change archived
- [ ] 5.3 更新 `SCALING_ROADMAP.md` 与 `PROJECT_CHECKLIST.md`：仅在 evidence 齐全（merge commit + 可复现测试/基准）时把 C1B/C1C 标 `verified`，Phase 1B 移入已完成节。验证：两文档状态与 evidence 一致、`verified` 满足 §8 规则
