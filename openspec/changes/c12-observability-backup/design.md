# C12 observability/privacy + backup manifest — 设计冻结（P-1）

> 输入决策：PILOT_ROADMAP §5.9.17（observability 与 privacy 默认值）、§5.9.12（persistence ownership 与 backup manifest）、§7/§7.1（验收指标与必采集数据）、§10 DECIDED「Persistence ownership」、§10 PROPOSED DEFAULT「日志与审计保留」、§10 DEFERRED BY EVIDENCE「SLO/容量阈值」。本文不重复论证这些决策，只记录把决策落成契约原语时的 ADR 级选择。

## 1. 契约交付形态

C12 无左依赖（§5.9.10：「12 可以先定义契约并伴随各 change 落地」）。本 change 的产物分两层：

- **规范层**：`specs/observability-privacy/spec.md`、`specs/backup-manifest/spec.md`（行为契约）+ `design.md`（ADR）+ fixture（单一来源 schema）。
- **可执行契约原语层**：redaction、label 白名单、retention sweep、manifest 校验器——它们本身就是 spec 可验证性的执行体（负向测试直接跑在这层上），但不接入任何业务路径。

「伴随落地协议」：每个 Cxx change 的 tasks checklist 必须含三条——(a) 按 `tests/fixtures/observability_event_schema.json` 实现其 §7.1 指标字段；(b) 所有新日志/trace/审计落点过 `core/telemetry/redaction.py`；(c) 在 `tests/fixtures/backup_manifest_template.json` 登记其 canonical store 条目并通过 manifest 校验器。该协议写入 task-12 并由本 change 的契约测试固定 fixture 形态。

## 2. ADR 记录

### ADR-1 content-off 的执行点 = 采集 emit 前 gate，不是存储侧过滤

**结论**：内容类字段（raw provider payload、完整 prompt、message content、tool args/result、attachment content）默认在采集调用点被拒绝——`ContentCaptureGate` 默认关闭，emit 侧先问 gate，关闭时内容字段直接丢弃并递增丢弃计数，只保留结构化 lifecycle metadata。开启 debug 采集时，内容也必须先过 redaction 再入任何持久面。

**备选**：(a) 存储侧过滤（全量落盘、读取时裁剪）——不选：一旦落盘即形成泄露面，与 §5.9.17「默认关闭」冲突，且磁盘/备份链路都会携带内容；(b) 仅靠代码规范/评审——不选：无机器可验证据，task-12 验收第 1 条要求负向测试。

### ADR-2 debug 采集开关 = admin-only + 短 TTL + 开启即审计

**结论**：`ContentCaptureGate.open(principal, reason, ttl)` 三要素缺一不可：开启者 principal、开启原因、自动过期时间（默认 300s，上限 3600s）；`open()` 同步产生一条 admin 访问审计事件（action=`enable_content_capture`）。gate 为进程内状态（Pilot 单进程，§9 决策），不持久化——重启后默认回到关闭态是 fail-safe 语义。

**备选**：持久化开关（配置文件控制）——不选：静态配置无法表达「短期」，容易变成常开；TTL 过期由读取时惰性检查实现（`is_open()` 比对单调时钟），不依赖后台定时器。

### ADR-3 redaction = 键名启发式 + 内容模式正则的确定性替换，fail-safe 为占位符

**结论**：四类规则，全部确定性、无 LLM 参与：

1. `secret`：键名启发式（`token`/`secret`/`password`/`api_key`/`authorization`/`cookie`/`credential` 等，词边界匹配）+ 值模式（Bearer 前缀、`sk-` 开头长串）；
2. `credential_url`：URL userinfo（`scheme://user:pass@host`）；
3. `local_path`：Windows 盘符路径（`C:\...`）、UNC（`\\...`）、POSIX 绝对路径中命中用户目录/工作区特征的（`/home/`、`/Users/`、`/root/`、`~/.nexus`、`/opt/`、`/tmp/`）；
4. `pii`：email、国际/中国手机号。

命中替换为类型化占位符（如 `[REDACTED:secret]`），保留长度类别信息便于 debug 但不保留原文。`redact_value` 对 mapping/list 递归（深度上限 8、条数上限 1000，防构造爆炸），非字符串标量原样返回（它们是 metadata）。任何规则执行异常 → 该字段整体替换为 `[REDACTED:error]` 并计数，绝不把原文放行。

**备选**：(a) 白名单提取（只允许已知安全字段通过）——作为长期方向更强，但 Pilot 观测字段集还在随 Cxx 扩展，白名单维护成本先于收益；label 白名单（ADR-4）已承担了 metrics 侧的白名单职责，日志/trace 侧先黑名单替换 + content-off gate 双保险。(b) LLM 辅助脱敏——不选：不确定性引入新的泄露面与成本，且 §5.9.17 要求入库前脱敏是确定性动作。

### ADR-4 metrics label 白名单 + 注册期校验；tenant_id 在 Pilot 规模内有界

**结论**：`core/telemetry/label_policy.py` 维护显式 allowlist（work_kind、flow、stage、trigger、channel、tool_name、model、provider、backend、status、result、error_type、retryable、side_effect、outcome_status、tenant_id、snapshot_id…完整集见 fixture），`PolicyCheckedMetricRegistry` 包装 `MetricRegistry`，注册时 label_names ⊄ allowlist 即抛错——高基数字段（account_id、message_id、tool_call_id、turn_id、work_id、session_key）与内容字段（*_args、*_content、*_prompt、raw_*）在禁止集，注册即失败。现有 C0 指标族（label：`channel`、`backend`）在 allowlist 内，兼容不破坏。

**tenant_id 边界**：§7.1 要求按 tenant_id 聚合，§5.9.17 禁止的是「account/message/tool-call 高基数字段」。Pilot 规模为 10–30 受邀账号（§2），tenant_id 基数有界，允许作为 metrics label；若未来规模越过 Pilot 边界（P4 闸门），须复评本 allowlist。account_id 与 tenant_id 在 Pilot 内 1:1，仍分列：account_id 进禁止集（语义上是「账号」维度，§5.9.17 点名禁止），tenant 视角聚合用 tenant_id。

**备选**：事后扫描导出结果——不选：导出后拦截为时已晚（Prometheus 抓取已发生），注册期 fail-fast 才可机器验证；且事后扫描无法阻止 label 值携带内容。

### ADR-5 retention 三档默认值 = 接受 §10 PROPOSED DEFAULT（30/180），内容型 debug 取 7 天

**结论**：`RetentionPolicy(operational_days=30, audit_days=180, debug_content_days=7)`，全部可配置（dataclass 字段），P-1 接受 PROPOSED DEFAULT 并补充内容型 debug 默认 7 天（§5.9.17 只说「更短独立 TTL」，本 change 冻结初始值为 7，P0/P3 可调）。清理 job `sweep_roots()` 按类别根目录做 mtime 过期删除：幂等（重跑零删除）、dry-run 支持、目录缺失降级为空扫描、删除失败逐条记录不中断。本轮先覆盖文件型观测产物（trace dump、debug 落盘文件）；PG 行级 retention 伴随 C2 表落地（Non-Goal 已声明）。

**备选**：立即实现 PG 行级 retention——不选：目标表不存在（C2 未开工），提前实现必然返工。

### ADR-6 backup manifest = 声明式 JSON + 校验器；模板即 Pilot 现状声明

**结论**：manifest 是版本化 JSON 文档（`manifest_version`、`created_at`、`entries[]`、`restore_order[]`），每条 entry 必含 `id`/`kind`/`path`/`encryption`/`checksum`/`consistency_point`/`retention`。校验规则（全部负向可测）：

1. kind 集合必须覆盖 `postgresql`、`tenant_workspace`、`config`、`secrets`（canonical store 全集；attachment blob root 属 tenant_workspace 命名空间，C6 落地时细化条目）；
2. 任何 entry path 命中临时目录（POSIX `/tmp/`、Windows `%TEMP%`）→ 拒绝（「`/tmp` 不属于 durable backup 范围」）；
3. `legacy_sqlite` entry 必须声明 `restore_mode="legacy_only"` 且 `independent=true`（只恢复到 legacy 模式，不恢复进 Pilot PostgreSQL）；
4. `secrets` entry 必须与普通业务备份分离（独立 entry、独立 `encryption` 声明）；业务 entry 声明 `contains_secrets=true` 而未独立成 secrets entry → 拒绝；
5. `restore_order` 必须恰好覆盖全部 entry id（恢复顺序完整性）；
6. 每个 entry 必须有一致性点（`consistency_point`）与校验（`checksum`）声明，缺任一 → 拒绝。

模板 fixture `tests/fixtures/backup_manifest_template.json` 声明 Pilot 现状（postgresql、legacy_sqlite、workspace 占位待 C6 细化、config、secrets），是后续 change 的登记起点。

**备选**：manifest 用 Python 代码声明——不选：manifest 的消费方包括恢复 runbook 与人工演练（P3），JSON 文档可独立于代码被校验、评审与归档。

### ADR-7 事件 schema 单一来源 = fixture，Cxx 伴随落地按契约测试对齐

**结论**：`tests/fixtures/observability_event_schema.json` 转录 §7.1 的字段表（身份与归属/分类/版本/生命周期时间/生命周期结果/副作用与恢复/资源七类）、派生耗时公式（queue_wait_ms 等 8 项）、聚合维度与禁止内容字段清单，另含 admin 访问审计事件 schema（viewer principal、target tenant、action∈{view_content, drill_down, export, enable_content_capture}、reason、at）。契约测试断言 fixture 与 §7.1 冻结字段一一对应、禁止内容字段与 label 禁止集交叉一致。C2/C3/C6 等落地事件字段时，以其实现与该 fixture 的 diff 作为评审输入。

**备选**：schema 定义在 Python 模块里——不选：fixture 可被前后端与跨 change 测试共享（仓库既有惯例：`tests/fixtures/chat_protocol_frames.json`、`canonical_identity_chain.json`），且任务拆分包明确要求「契约 fixture」。

### ADR-8 SLO 数字 DEFERRED BY EVIDENCE，静态负向测试防止阈值进入代码

**结论**：本 change 不写任何 P95/失败率/RPO/RTO 数字红线；负向静态测试扫描 `core/telemetry/` 与 `core/backup/` 不存在 SLO 阈值常量（如 `P95`/`SLO_` 命名的判定逻辑）。基线报告（P0 后期）产出后，由后续 change 以配置项引入阈值。

**备选**：先冻结经验值——不选：§10 明确 DEFERRED BY EVIDENCE，编码前拍数字违反 roadmap。

## 3. 数据流与模块边界

```text
emit 侧（Cxx 伴随落地）
  ├─ 结构化 lifecycle metadata ──→ metrics（PolicyCheckedMetricRegistry 校验 label）
  ├─ trace/audit metadata ──────→ redact_value() → 持久面
  └─ 内容类字段 ──→ ContentCaptureGate
        ├─ 默认关闭 → 丢弃 + 计数（负向测试锚点）
        └─ admin 开启（principal+reason+TTL+审计事件）→ redact 后入 debug 存储（独立短 TTL）
retention sweep ──→ 按 operational/audit/debug_content 三档根目录清理
backup manifest ──→ 模板 fixture + 校验器（每个 Cxx 登记条目时复跑）
```

本 change 不新增任何 emit 侧调用点（不改既有运行路径）；Cxx 落地时接线。

## 4. Risks / Trade-offs

- **黑名单 redaction 存在漏网内容**：正则启发式不可能穷尽内容形态。缓解：content 默认关闭（第一道防线）+ label 白名单（第二道）+ redaction（第三道）纵深，而非单点依赖；debug 内容独立短 TTL 进一步限损。已知接受。
- **tenant_id 进 metrics label 的规模边界**：Pilot 10–30 账号内有界；越界（P4）需复评。已在 ADR-4 声明为显式边界而非疏忽。
- **manifest 模板与实际 store 漂移**：伴随落地协议把「登记条目 + 复跑校验器」绑进每个 Cxx 的 checklist；校验器的 kind 覆盖检查兜底。风险：协议靠流程遵守，无强制钩子——接受（Pilot 单仓流程内可控）。
- **retention sweep 只覆盖文件型**：PG 行级 retention 缺位由 C2 承接；在 spec 中明确归属避免遗漏。
- **ContentCaptureGate 进程内状态**：多副本部署（P4 后）失效，但 Pilot 单进程（§9 决策）内正确；gate 不持久化是刻意的 fail-safe。

## 5. Rollback 策略

本 change 全部为新增独立模块 + fixture + 测试，无既有路径修改、无 DB 变更、无配置变更。回滚 = revert 对应 commit；fixture 与 change 文档随仓库版本管理，无独立状态需要清理。后续 Cxx 若已基于契约实现，回滚本 change 不影响其已合入代码，但会失去校验器——按「契约先行」原则，届时应保留模块只回滚行为变更。
