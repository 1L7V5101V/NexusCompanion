# task-2.1 / 2.2 / 2.3 — 配置节与加载校验

日期：2026-10-03

## 变更

| 文件 | 内容 |
| --- | --- |
| `agent/config_models.py` | **新** `RetentionConfig`：`enabled=True`、`interval_s=86400`（0=不启用周期任务）、三档天数（引用 `core.telemetry.retention` 冻结常量 `OPERATIONAL/AUDIT/DEBUG_CONTENT_DEFAULT_DAYS`，单一来源不重复写数字）、`batch_size=500`、`max_batches=20`、`purge_grace_s=30*86400`（暂定）、`replay_keep_last_frames=20`（暂定）、`replay_max_age_days=30`（暂定）、`file_roots={}`（ADR-1 内置空）；挂 `Config.retention` + `__all__` |
| `agent/config.py` | **新** `_load_retention_config(data)` 读 `[agent.retention]`，沿用 `_load_attachment_config` 校验模式：天数/批次/下限帧/天花板必须正整数、`interval_s` 允许 0、`purge_grace_s` 非负；bool 与分数拒绝（`int(True)=1`/`int(1.5)=1` 静默通过是 footgun）；`file_roots` 只接受三档类别键、未知类别/空路径即报错；`load_config` 已接线 |
| `config.example.toml` | **新** `[agent.retention]` 注释块（默认值 + "默认开启即会删除超窗数据，上线前先跑 dry-run" + 文件 root 禁配活跃文件警告） |

## 测试证据（tests/retention/test_config.py，18 passed）

- `test_retention_config_frozen_defaults`：逐项断言 + 与 `RetentionPolicy()` 默认同源一致；
- 非法值加载期报错：0/负数/非数字/bool/分数逐项 `pytest.raises(ValueError, match=配置项名)`；
- `interval_s = 0` 合法（不启用周期任务）、`file_roots` 合法/非法形状分组断言；
- `config.example.toml` tomllib 解析成功且 `load_config` 走通（retention 块为注释不影响解析）。
- 汇总见 `task-8.1-targeted.txt`（`tests/retention/` 53 项全绿）。
