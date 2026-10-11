# P3 实机演练：schedule misfire / 重启 / 幂等 / DST（2026-10-11）

对应 [PILOT_ROADMAP §5.9.14](../../PILOT_ROADMAP.md) 与 PILOT_ROADMAP_PROJECT_CHECKLIST 的
「schedule misfire/restart/idempotency/DST 演练」条目。自动化侧此前已由 `tests/schedules/` 46 项固化；
本次补的是**生产机真实停机/重启**那一半。

## 环境

- 部署机 `/opt/NexusCompanion`，compose 项目 `nexuscompanion`，应用容器 `NexusCompanion`，库容器 `nexus-postgres`
- 代码 `main@028163a7`，由流水线 run [38115564388](https://github.com/1L7V5101V/NexusCompanion/actions/runs/38115564388) 发出：
  types + tests 四片 + deploy 全部 success；探活 Dashboard `HTTP 200（等待 9s）`、WebChat 200、
  `DurableSchedulerService started（恢复扫描收束 0 条中断执行）`
- schema：`d8e4f2b6a9c1 → e8b4c2a6d9f1`（`scheduled_jobs` / `schedule_executions` /
  `scheduled_jobs_import_legacy`，幂等索引 `uq_schedule_executions_job_scheduled (job_id, scheduled_for)`）
- 演练用租户 `dev`（account `...0001` / conversation `...0002`），`tier=instant` 不触发 LLM
- 时间口径：下表全部为 UTC（容器墙钟 Asia/Shanghai，13:52 ≈ 05:52Z）
- 为把「超宽限」的停机代价压到 2 分钟内，演练期间临时在生产 `config.toml` 加
  `[scheduler] misfire_grace_seconds = 60`（默认 300）；演练后已从备份
  `/root/nexus-deploy-state/config.toml.pre-drill-20261011-134931.bak` 还原并重启，运行值回到 300。

## 四条腿的实测数字

| 腿 | 造的东西 | 停机动作 | 实测结果 |
| --- | --- | --- | --- |
| A 宽限内 misfire | `at` 05:52:59 | 触发前 5s stop，45s 后 start | `started_at 05:53:22.264` = **迟到 23.3s（<60s 宽限）→ 补发**，`status=succeeded`，`attempt_count=1`，产出 1 条 intent |
| B 超宽限 | `at` 05:56:10 | 触发前 stop，175s 后 start | `status=missed`、`skip_reason=misfire_grace_exceeded`、`started_at IS NULL`、**零 intent**，job `last_outcome=missed` |
| C recurring 认领时前进 | `every 45s` | 正常运行期观察 | 三次 occurrence：`06:02:12.423689` / `06:02:57.423689` / `06:03:42.423689`，**间隔严格 45.000000s 且亚秒余数恒定**（由锚点推算、不跟墙钟漂），`next_scheduled_for=06:04:27.423689` |
| D DST 重复时刻 | `at 2026-11-01T01:30` tz=`America/New_York`（回拨造成的歧义墙钟） | 无需触发 | 服务端冻结 `next_scheduled_for = 2026-11-01 05:30:00+00:00` = **首次出现（01:30 EDT）**，与 contract「取首次、不双触发」一致 |

**幂等**：四条腿 + 三次真实停机/重启之后，
`select job_id, scheduled_for, count(*) ... group by 1,2 having count(*)>1` 返回 **0 行**。

## 顺带在生产上确认为「设计如此」的两点

- WebChat 收件人不在线时 intent 落 `status=failed` 且 `next_attempt_at` 有值 → **退避重试**而不是立即死信；
  重放帧已在同事务写入，离线端重连后经补拉/REST 重建补齐。
- one-shot 执行完成后 job 保持 `active` 且 `next_scheduled_for IS NULL`。job 状态词汇表
  （`active/suspended/revoked`）本就没有 completed 态，NULL next 即「不再被认领」的表达。

## 清理与残留

- 演练行按外键顺序整事务删除：jobs 4、executions 7、intents 6、replay_frames 6、delivery_attempts 12；
  删后 `dump` 的 P3- 集合为空。
- 中途两次 FK 失败（先漏 `delivery_attempts`、再漏 execution→intent 的引用顺序）都**整批回滚**，
  没有产生半删状态——这本身验证了清理走单事务是对的。
- 残留：演练写入 `dev` 租户的 assistant 会话消息**保留**（属会话历史，不随手删）。
- 观测到的既有数据：`outbound_delivery_intents` 里有 2 条早于 C11 的 `dead_letter`，
  其中一条 `last_error` 为「replay frame 缺失（retention?）」。与本次发版无关，留作运维线索。

## 复现口径

造 job / 读状态 / 清理用的是一次性运维脚本（未入库，本次会话临时文件），全部走
`ScheduleRepository.create_job`、`resolve_local_wall` 等生产函数，未自行复刻任何计算；
状态核对一律只读 SQL。停机动作为 `docker stop -t 2` / `docker start`，与真实崩溃重启等价路径
（启动都会跑 `sweep_interrupted_executions`，本次收束 0 条中断执行）。
