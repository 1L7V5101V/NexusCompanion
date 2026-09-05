# Task-12 — observability/privacy/redaction + backup manifest（observability-backup）

> 编号对应 PILOT_ROADMAP §5.9.10 第 12 项。状态标记复用 §8。贯穿型 change：P-1 契约 → P0 基线 → P3 演练（D5 虚线贯穿）。

## 元数据

- **所属阶段**：P-1 契约定义 → P0 基线 → P3 演练（贯穿，D5）
- **§5.9 引用**：§5.9.17（observability 与 privacy 默认值）、§5.9.12（persistence ownership 与 backup manifest）、§7（验收指标与必采集数据）、§10 PROPOSED DEFAULT（日志与审计保留）、§10 DECIDED（Persistence ownership）
- **§6 出口条件引用**：P0 出口「当前 persistence map、backup manifest、健康检查与基础指标」「默认日志执行 secret、PII 和本地路径脱敏」；P3 出口「从备份恢复演练」「总控制台聚合缓存命中率/Token 消耗/错误率/延迟/在线数/队列状态」
- **状态**：in_progress（P-1 契约层已交付：change `c12-observability-backup`，2026-09-06，证据 `evidence/c12-observability-backup/`；§8 伴随落地条目与 P0/P3 出口项未完成，全项 `verified` 需等基线报告与恢复演练）

## 目标

定义采集规范 + 事件 schema（§7.1：work_kind/flow/stage/backlog/latency/skip/failure/timeout/tenant_id/session_key；任务生命周期事件；按维度聚合；数据保留与验收规则）；default content-off + redaction（**默认只采集结构化 lifecycle metadata**；raw provider payload/完整 prompt/message content/tool args/attachment content 默认关闭；入库前做 secret、PII、本地路径和 credential redaction；metrics label 不含高基数/原始 args）；retention（operational 30d / audit 180d / 内容型 debug 更短，§10 PROPOSED DEFAULT）；backup manifest 覆盖 PG / tenant workspace blob root / 配置 / secret + 一致性点/加密/校验/恢复顺序；legacy SQLite/workspace 独立备份只恢复到 legacy 模式；`/tmp` 不属 durable；dashboard 全局总控台聚合（按 work_kind/flow/stage/tenant/channel/model）；SLO 数字归 DEFERRED BY EVIDENCE，先采集后设。

## 输入

- 上游 change 产出：C1/C2/C3 产出的 work/turn/tool/delivery id 字段（E10）；C6 的 blob root（manifest 条目）
- roadmap 冻结决策：§5.9.17、§5.9.12、§7（必采集 + 验收规则）、§10 DECIDED Persistence ownership、§10 PROPOSED DEFAULT 日志/审计保留
- 现有代码锚点：`core/telemetry/`（metrics / metrics_export / trace_store，**已 verified 可复用**）
- 依赖前置：无（先定义契约；伴随各 change 落地，D5）

## 输出

- 规范：采集规范 + 事件 schema（§7.1）、redaction 规则
- 代码：redaction 实现、retention job、backup manifest 模板 + 校验、dashboard 全局总控台聚合 API、基线报告模板
- 契约 fixture：指标 label 规则契约（无高基数/原始 args）、manifest 模板 fixture
- 测试/证据：采集负向测试、label 规则测试、retention job 测试、manifest 校验、恢复演练报告（P3）、基线报告、audit 测试、聚合 API 测试

## 验收标准

- [ ] 默认只采集结构化 lifecycle metadata；raw payload/prompt/tool args/attachment content 默认关闭（admin 开关短期、审计化、入库前脱敏） — 验证：采集负向测试
- [ ] metrics label 无 account/message/tool-call 高基数字段/原始 args/内容 — 验证：label 规则测试（§5.9.17）
- [ ] retention 可配置且 job 生效（operational 30d / audit 180d，§10 PROPOSED DEFAULT 于 P-1 复核） — 验证：retention job 测试
- [ ] backup manifest 列全 canonical store（PG / workspace blob / 配置 / secret）+ legacy SQLite 独立项 + 恢复顺序 + 一致性点/加密/校验 — 验证：manifest 校验（含 C6 blob root 覆盖检查）
- [ ] P3 演练：从备份恢复 + PITR + attachment orphan/missing reconciliation — 验证：恢复演练报告
- [ ] 基线报告（前两周/首批稳定运行后）含 backlog/latency/failure rate/cancellation/compensation/recovery window — 验证：基线报告产出（§7）
- [ ] admin 内容查看/跨 tenant 下钻/导出产生 audit event — 验证：audit 测试（§5.9.17）
- [ ] 总控台按 work_kind/flow/stage/tenant/channel/model 聚合 — 验证：聚合 API 测试
- [ ] SLO/容量阈值**不在编码前冻结**（DEFERRED BY EVIDENCE），先采集后设 — 验证：基线报告用于 SLO 制定

> 判定「真正完成」而非「执行过」：采集负向测试证明 content 默认关闭；manifest 校验证明 canonical store 全覆盖；恢复演练报告是 P3 出口证据。本 task 为贯穿型（D5），每项 Cxx 落地时同步实现其 §7.1 指标字段 + redaction + backup manifest 条目（落地协议见下）。

## 独立性边界（不与其他任务重复）

- 本任务拥有：采集规范/事件 schema、redaction、retention、backup manifest 模板 + 校验、总控台聚合
- 本任务不触碰：具体业务表 schema（各 Cxx 拥有）；只定义「每个 change 落地时同步实现其指标/redaction/backup 条目」
- 共享 seam 协议：**伴随落地协议** — 每个 Cxx change 的 checklist 必须含「§7.1 指标字段 + redaction + backup manifest 该 canonical store 条目」；总控台消费各 Cxx 产出的指标

## 依赖

- **左依赖（必须先完成）**：无（先定义契约，D5）
- **右依赖（本任务前置于）**：无（贯穿所有 Cxx；P3 演练收尾）
- **可并行**：全部（先定义契约，伴随各 change 落地；E10 依赖 C1/C2/C3 的 id 字段在其实现后同步采集）

## 风险与需冻结决策

- §10 PROPOSED DEFAULT「日志与审计保留」：P-1 接受或调整 retention、访问审批和删除 job；**指标 label 规则不可放宽为内容采集**。
- §10 DECIDED「Persistence ownership」：每个 change 标明 canonical/compatibility/derived store；设计恢复一致性点和 secret 处理；legacy SQLite 独立备份只恢复 legacy 模式。
- §10 DEFERRED BY EVIDENCE：SLO/采样率/容量阈值由 Pilot 数据决定；先建基线报告，再设红线。
- 风险：C12 贯穿易遗漏 → 用「伴随落地协议」写入每个 Cxx checklist；`/tmp` 误入 backup → manifest 校验排除；SLO 提前拍脑袋 → 验收第 9 条强制 DEFERRED。