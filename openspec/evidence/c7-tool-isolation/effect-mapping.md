# C7 task 1.2 — effect 等级逐工具映射表（ADR-4）

> 现状 risk 标签来源：`scripts/tool_inventory.py` 装配点扫描（见 tool-inventory.json）。
> effect 初值依据 design ADR-4：白名单内无 `external-write`；`write`（租户内写）→
> `tenant-local-write`；`external-side-effect` 且服务端绑定/只读外联 → `network`；
> 关闭面按最高危等级归类。`requires_compensation` = 是否需幂等/outcome/补偿登记
> （external-write/process-exec/admin 无登记即默认拒绝，ADR-4）。

## 允许清单（P1 白名单，普通 tenant 可见）

| tool id | 现状 risk 标签 | C7 effect | requires_compensation | 备注 |
| --- | --- | --- | --- | --- |
| `recall_memory` | read-only（default/rachael profile 同值） | `read-only` | 否 | engine 注入路径同值 |
| `memorize` | write（default profile） | `tenant-local-write` | 否 | 租户内记忆 |
| `forget_memory` | write（default profile） | `tenant-local-write` | 否 | 同上 |
| `search_messages` | read-only | `read-only` | 否 | 强制本账号/租户过滤 |
| `fetch_messages` | read-only | `read-only` | 否 | 同上 |
| `read_file` | read-only | `read-only` | 否 | 走 TenantPathResolver（task 5） |
| `list_dir` | read-only | `read-only` | 否 | 同上 |
| `write_file` | write | `tenant-local-write` | 否 | 限租户目录；临时文件+原子替换 |
| `edit_file` | write | `tenant-local-write` | 否 | 同上 |
| `read_image_vision` | read-only | `read-only` | 否 | 复用文件 scope |
| `message_push` | external-side-effect | `network` | 否 | 仅服务端绑定目标（ADR-3/4）；若未来放开任意目标须改 `external-write` + 补偿登记 |
| `schedule` | write | `tenant-local-write` | 否 | tenant-owned（C11 细化归属） |
| `remind` | write | `tenant-local-write` | 否 | 同上 |
| `list_schedules` | read-only | `read-only` | 否 | 仅本租户 |
| `cancel_schedule` | write | `tenant-local-write` | 否 | 仅本租户 |
| `web_search` | read-only | `network` | 否 | 限流 + SSRF 防护（task 3.3） |
| `web_fetch` | read-only | `network` | 否 | 同上 |
| `tool_search` | read-only | `read-only` | 否 | 搜索范围 = 当前租户可见目录 |
| `reinforce_memory`（rachael 注入） | write（MemoryToolSpec.risk） | `tenant-local-write` | 否 | ADR-3 类别规则；effect 按引擎 risk 如实映射 |

## 关闭清单（普通 tenant 不可见不可执行）

| tool id | 现状 risk 标签 | C7 effect（如 admin 误配/直调） | 备注 |
| --- | --- | --- | --- |
| `shell` | external-side-effect | `process-exec` | 普通 tenant 拒绝 |
| `spawn`（含 `delegate_*` 动态名） | write | `process-exec` | 同上（注册点 bootstrap/toolsets/meta.py:89） |
| `spawn_manage` | external-side-effect | `process-exec` | 同上 |
| `task_output` | read-only | `read-only`（catalog 关闭面 + owner 校验，6.2） | 随 spawn 关闭：closed 清单不可见 + 仅 own 租户任务；创建面（shell）本就 process-exec 关闭 |
| `task_stop` | external-side-effect | `process-exec` | 同上 |
| `load_skill` | read-only | `admin` | skill/插件管理面 |
| `mcp_add` | external-side-effect | `admin` | workspace 级全局 MCP 管理 |
| `mcp_remove` | write | `admin` | 同上 |
| `mcp_list` | read-only | `admin` | 同上 |

## 运行时不注册（诚实清单）

- `AgentRestartTool`（`bootstrap/tools.py:499`）、`WorkspaceMcpApplyTool/RemoveTool/StatusTool`
  （`bootstrap/tools.py:777-780`）：注册点存在但模块文件缺失（import 失败静默跳过），
  运行时零注册；若未来补齐归 `admin`/admin-only。

## 校验说明

- 每行「现状 risk 标签」可由 `tool-inventory.json` 的 entries 交叉核对。
- 本表为 ADR-4 的落地初值；实现时若发现工具行为与 effect 不符（如某工具实际外呼），
  回 design 修 ADR-4 而不是就地改表。
- 第二双眼睛：2026-09-28 提交后复核已执行——修正两处表格初值与实现漂移：
  `task_output` 实现为 read-only（catalog 关闭面 + owner 校验，6.2）非 process-exec；
  `spawn`/`spawn_manage`（含 delegate_* 动态名）注册点为 process-exec 非 external-write。
  评审点 `message_push`/web 三件的 `network` 归类 + `requires_compensation=否` 确认无漂移
  （与注册点显式覆盖一致，4.1/3.3 测试锁定）。
