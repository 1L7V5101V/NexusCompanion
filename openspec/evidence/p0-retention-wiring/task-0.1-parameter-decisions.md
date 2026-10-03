# task-0.1 — owner 参数确认状态（诚实记录：未完全闭环）

日期：2026-10-03

## owner 决策折入历史

- **四项结构性决策**：已由 owner 确认并折入（git `adf282d7`，2026-10-02）——清理默认开启
  （每天一轮、开机不跑首轮、先 dry-run 存证再实删）、`work_attempts` 归 audit 180d、
  过期凭据处置保留本轮（expand-only 放宽）、补发缓冲改按会话补发窗口（ADR-8）。

## 三个数值的实施期状态

实施开始（2026-10-03）时 owner 对三个**数值**尚未答复。已通过 AskUserQuestion 向 owner
展示候选值与推荐项（`replay_keep_last_frames` 20/50/100、`replay_max_age_days` 30/60/90、
`purge_grace_s` 7d/30d/90d），**用户未作答**（目标模式异步执行）。

**处理**（记录于 design.md Open Questions）：按 design 候选值落为**可配置暂定默认**
——`replay_keep_last_frames=20`、`replay_max_age_days=30`、`purge_grace_s=30d`
（后者本就是 design 暂记值）。三个值全部是纯 config 参数（`[agent.retention]`），
owner 确认或改值**不需要改代码**；`config.example.toml` 注明"暂定默认、owner 确认前可调"。

## 本任务勾选状态

**保持未勾**，直到 owner 确认数值（或改 config 覆盖暂定默认）。
不把"实现自定默认"伪装成 owner 决策——这是 tasks 0.1 明确禁止的行为。

## 对验证项的对应

design.md Open Questions 三条已改写为"暂定值 + 待 owner 确认"状态（非移除）；
第 4 项（游标来源）已核实闭环（见 `task-3.1-cursor-source.md`）。
