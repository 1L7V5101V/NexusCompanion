## 1. 分支与上下文

- [x] 1.1 从 main 切独立分支（如 `docs/project-checklist`），确认分支存在且工作区干净（`git status`）；验证 `openspec validate` 当前通过（记录基线输出）
- [x] 1.2 通读依赖材料：`openspec/SCALING_ROADMAP.md`、`openspec/README.md`、`openspec/records/phase1-storage/*`、`openspec/evidence/phase1-storage/*`、`openspec/changes/archive/2026-08-23-establish-scaling-openspec-baseline/`，确认 checklist 条目与链接目标

## 2. 创建 PROJECT_CHECKLIST.md

- [x] 2.1 在 `openspec/PROJECT_CHECKLIST.md` 创建 checklist 主体：头部声明其派生观测面身份、source-of-truth 优先级与 §7/§8 状态规则引用（链接到 SCALING_ROADMAP，不复制定义）；验证文件存在且头部不含内联的完整 evidence
- [x] 2.2 添加"当前进度"小节（当前 phase、focus、blocker）：从 `SCALING_ROADMAP.md` §5 现状转写为简短状态叙述并链接回该文件，不改动 roadmap 内容；验证该小节存在且 roadmap 未被修改
- [x] 2.3 按 phase 分组添加 11 项能力 checklist 父条目（GOV、C1、C1B、C1C、C1D、C0、C2、C3、C4、C5、C6），每条 = `- [ ] 能力名 — 一行简短描述；状态标记；→ 详情链接`；verified 项（GOV、C1）勾选 `- [x]`，其余保持 `- [ ]`；验证每项包含指向 spec/change/record/evidence 的相对链接
- [x] 2.4 为已实现能力（GOV、C1）展开子任务条目：从 `openspec/records/phase1-storage/`（m4h-4-partition-provisioning / m4h-3-connection-isolation / m4h-2-tenant-wiring / m4.5-architecture-hardening 等）、`openspec/evidence/phase1-storage/` 与 `infra/storage/` 代码推导功能切片（如 C1 → SQLite/PG 双 adapter 与共同 interface / tenant 贯穿 / pool + bounded executor / provisioning 控制面 / merge readiness），每个子条目缩进在父条目下并链接到对应 record/evidence/代码；验证子条目为功能切片而非 M0/M1 任务编号镜像，且每个子条目链接目标存在
- [x] 2.5 为计划中能力（C1B-C1D、C0、C2-C6）展开子任务条目：从各自 capability outcome 描述拆出（如 C1B = 批量 COPY / 断点续传 / 机器可读校验），不虚构超出既有规划的粒度；验证子条目与 SCALING_ROADMAP §4 outcome 语义一致
- [x] 2.6 逐条校验链接目标存在（对 specs/records/evidence/changes 下被引用文件做存在性检查），修复失效链接；验证无死链

## 3. SCALING_ROADMAP.md 不改动（只建立 checklist 引用）

- [x] 3.1 确认 `openspec/SCALING_ROADMAP.md` 内容保持不变（git diff 为空）；验证 checklist 中引用的 §7/§8 状态规则与 §4/§5 现状与本文件实际一致
- [x] 3.2 在 checklist 头部加入指向 `SCALING_ROADMAP.md` 的跳转链接（总体目标与决策的权威来源），确认两文件互相引用一致；验证链接存在且 roadmap 未被修改

## 4. 更新 README.md 入口

- [x] 4.1 更新 `openspec/README.md` 内容入口表：新增 `PROJECT_CHECKLIST.md` 行（定位：完整项目进度观测 checklist，phase 细化的展开面），不改动 `SCALING_ROADMAP.md` 行描述；验证表格条目与文件实际存在一致

## 5. 校验与收尾

- [x] 5.1 运行 `openspec validate`，确认通过且无 skipped_specs 相关报错；记录输出路径 `openspec/evidence/` 或本地临时日志
- [x] 5.2 复查文件（checklist/README）交叉链接与用语一致（能力名、状态词、链接路径），无跨文件矛盾；复核 `openspec/SCALING_ROADMAP.md` 未被修改（git diff 为空）；验证 checklist 状态与 §7/§8 规则一致
- [x] 5.3 提交到分支（无 Co-Authored-By、无 emoji），记录 commit hash；如需合并回 main 时执行 merge 并记录 merge commit 作为 evidence，写入 change tasks checkbox 与 `openspec status`
