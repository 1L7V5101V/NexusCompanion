# task-3.1 — 「已确认消费」游标来源核实

日期：2026-10-03　结论：**采纳客户端 `replay.after_seq` 为消费游标，新增最小持久化**
（`webchat_replay_counters.consumed_seq` 列）；未触发「停下报告」条件，因为现有游标中
确实存在语义完全等于「客户端已收到」的值，只是此前用后即弃。

## 候选游标逐一核实（代码出处）

| 候选 | 出处 | 语义 | 是否「客户端已收到」 |
| --- | --- | --- | --- |
| `current_seq()` | `bootstrap/db/repository/control_plane_repo.py:956`（读 `WebchatReplayCounterModel.next_seq - 1`） | **服务端写入水位**：已提交帧计数，与 C2 三事务绑定 | ✗（服务端写了 ≠ 客户端收到） |
| `oldest_seq()` | `control_plane_repo.py:972` | 仍保留的最旧帧 seq（retention 窗口下沿） | ✗（窗口边界，与消费无关） |
| hello `latest_seq` | `infra/channels/web_chat_protocol.py:108-132`、`web_chat_channel.py:342-364`（服务端 → 客户端方向） | 服务端水位下发给客户端做对齐 | ✗（同为服务端水位，仅方向相反） |
| 客户端 `replay {after_seq}` | `web_chat_channel.py:437-455`（入站分发）→ `_handle_replay_durable` → `WebchatDurableService.replay_after`（`bootstrap/webchat_durable.py:108-128`） | 客户端声明「我已收到 ≤ after_seq」，服务端按 `seq > after_seq` 补发 | **✓ 语义精确匹配** |

关键事实：客户端游标**只在 replay 请求内存中出现一次，从不落库**（全库 grep
`after_seq`/`latest_seq` 无任何持久化写点）；`replay_after` 的窗口判定（`after_seq <
oldest - 1` → `replay_required`）本身就是以该游标为"客户端已收到"的语义在消费。

## 结论与落地方案（写回 design ADR-8）

1. **消费游标 = 客户端 `replay.after_seq`**（唯一的"客户端已收到"声明点）。
2. **最小持久化**：`webchat_replay_counters` 增列 `consumed_seq BIGINT NULL`
   （expand-only 迁移，第二个 revision）；durable replay 处理时
   `consumed_seq = GREATEST(consumed_seq, LEAST(after_seq, current_watermark))`。
   - GREATEST：游标只进不退；LEAST：客户端声明超前于服务端水位（异常/篡改）不推高游标。
   - NULL = 从未有客户端声明（该会话没发过 replay）→ 不产生消费侧删除，只走
     兜底天花板 + 下限帧保护。
3. **为何必须有此列**：spec「已消费的旧帧可被裁剪」Scenario 要求消费态参与删除判据；
   无持久化游标则该 Scenario 无法满足（年龄 ≤ 兜底天花板的已消费帧永远无法先于天花板被裁）。
4. **多设备 caveat（记入 design Risks）**：游标为 per-conversation MAX——同一会话多设备时，
   落后设备的未消费帧可能因另一设备的游标被裁，重连走既有 `replay_required` → REST 重建
   降级（`bootstrap/webchat_durable.py:449-452` 既有语义，canonical 仍为真源，无数据丢失）。
   Pilot 以单账号单设备为主，正式放量前与 C1D 一并复评（与 design 风险条目同口径）。

## 对 tasks/design 的回写

- design.md ADR-8 增补游标机制与多设备 caveat；Open Questions 第 4 点（游标来源）标记已核实。
- 第 1 节迁移外的第二个 revision：`consumed_seq` 列（随 sweeper 实现提交，证据并入
  `task-3.1-pg-sweeps.md`）。
