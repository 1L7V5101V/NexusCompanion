# task-7.3 — C12 §8.4 承接登记

日期：2026-10-03

## 登记（写法参照 §8.3 由 C6 承接的先例）

`openspec/changes/c12-observability-backup/tasks.md` §8.4 行尾追加：

> **2026-10-03 追加：retention 半边已由 change `p0-retention-wiring` 承接落地**
> （config `[agent.retention]` + `RetentionRuntime` 周期壳 + PG 行级裁剪 + 过期凭据
> 摘要抹除 + dry-run 演练入口 `scripts/retention_run_once.py`；新增 capability
> `data-retention`），**本条目保持未勾**——总控台聚合 API 半边仍待指定 change
> （写法同 §8.3 由 C6 承接的先例）

## 双向引用

- C12 §8.4 → 本 change（上文登记）；
- 本 change tasks.md 头部「上游义务」节 → C12 §8.4（proposal/design/tasks 均有）；
- 本 change 归档时 C12 §8.4 的 retention 半边语义完成，但**整条不勾**
  （聚合 API 半边未落地，与 §8.3"C6 承接后 C12 侧才勾"的节奏一致——8.4 需两个半边
  都有承载时才整体关闭，避免半边完成被误读为条目完成）。

## 验证

```
openspec validate c12-observability-backup  → Change 'c12-observability-backup' is valid
openspec validate p0-retention-wiring       → Change 'p0-retention-wiring' is valid
```
