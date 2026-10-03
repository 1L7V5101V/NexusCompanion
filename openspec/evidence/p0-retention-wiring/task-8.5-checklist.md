# task-8.5 — 状态回填（在 8.1–8.4 有证据后执行）

日期：2026-10-03　凭据：`task-8.1-targeted.txt` / `task-8.2-full-regression.txt` /
`task-8.3-pyright.txt` / `task-8.4`（openspec validate 双双 valid）

## 回填明细

1. `openspec/PILOT_ROADMAP_PROJECT_CHECKLIST.md`：
   - §5.9.17 门禁行（line ~89「结构化日志 redaction 与默认 content-off 基线」）：
     "config 接线与进程内定时执行归 C12 §8.4" → **执行层落地注记**
     （change `p0-retention-wiring`，2026-10-03，证据链接；config 接线 +
     RetentionRuntime 定时执行 + PG 行级分批裁剪 + 凭据摘要抹除 + dry-run 入口 +
     可恢复性分类报告）。
   - **current focus**：8.4 标注 retention 半边已落地（总控台聚合 API 半边仍开放）。
   - **next decision**：①的 §8.4 归属更新为只剩聚合 API 半边待指定 change。
2. `openspec/openspec-tasks-bundle/task-12-observability-backup.md`：
   验收标准「retention 可配置且 job 生效」行补注执行层落地（契约层语义不变，
   补执行层 change、ADR-4/ADR-8 迁移、测试与回归数字；总控台聚合 API 仍归 §8.4
   另一半边）。
3. `openspec/SCALING_ROADMAP.md`：**未改动** —— grep 全文无 retention/保留期能力行
   （仅一处无关的叙述性"保留"用词），按 tasks 8.5 "仅在存在对应能力行时更新" 条款跳过，
   本文件即为说明。

## git 证据

回填以本 change 分支内 commit 提交（`git diff main...HEAD -- <文件>` 可见）。
