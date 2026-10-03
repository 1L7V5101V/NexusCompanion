# task-3.6 — 补发窗口四 Scenario 用例对照

日期：2026-10-03　测试文件：`tests/retention/test_replay_window.py`（6 passed）

| spec Scenario | 用例 | 断言要点 |
| --- | --- | --- |
| 未消费的旧帧不因年龄被删 | `test_unconsumed_frames_within_ceiling_not_deleted` | 5 天前写入（<30d 天花板）、无消费声明 → 一轮后 5 帧全在，`frames_after(0)` 连续 |
| 已消费的旧帧可被裁剪 | `test_consumed_old_frames_trimmed_beyond_floor` | 40 帧全消费（游标 40）→ 删 seq 1..20（超出 keep_last=20 下限），21..40 保留；游标 20 处重连补拉仍连续（21..40） |
| 长期离线会话受兜底上限约束 | `test_long_offline_session_hits_ceiling_and_reports` | 40 帧全 60 天前、无消费声明 → 超天花板部分除下限外被裁（20 帧）、`bytes_freed` 如实入报告；读侧 `oldest_seq=21`，游标 0 < oldest-1 → 走既有 replay_required/REST 重建语义（对照 `test_webchat_rebuild_reconcile.py`） |
| 每个会话保留帧数有下限 | `test_floor_keeps_last_n_when_all_consumed_and_old` | 25 帧全消费 + 全 90 天前 → 仍保留最近 20 帧（删 5） |

附加守卫：

- `test_consumed_cursor_greatest_and_capped_by_watermark`：游标 GREATEST 只进不退、
  超前声明被水位封顶、空会话记录为 no-op（task 3.1 持久化语义）；
- `test_window_isolation_between_conversations`：会话 A 全消费可裁，会话 B（未消费）
  同轮零删除——裁剪判据确以**各会话自身补发进度**为准（spec SHALL NOT 全局一刀切）。
