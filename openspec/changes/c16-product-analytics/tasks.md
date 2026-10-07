# C16 产品度量（product analytics）— 实施任务

> 对应 `openspec/changes/c16-product-analytics/`；design.md D1..D7 为实现约束，偏离需先改 design。
> 分支：`feature/c16-product-analytics`（基于 main tip）。
> 证据统一落 `openspec/evidence/c16-product-analytics/`（每条任务给出命令与结果）。
> 环境：本地 PG 5433（Docker 实例）；集成测试 `NEXUS_TEST_PG_URL` + `NEXUS_REQUIRE_PG=1`
> 走独立 scratch 库（模式同 `tests/retention/conftest.py` / C9 / C10 先例）。
> 前置：C1/C2/C5（登录会话与 admin principal）✅、C6（附件）✅、C9（persona 审计）✅、
> C10（跨端绑定）✅、C15（工作项与投递尝试）✅ 均已归档；C12 契约层已落码
> （`core/telemetry/label_policy.py`、`core/telemetry/audit.py`），其 spec sync 时机不阻塞本 change（D2 备注）。
> 本 change 无 alembic 迁移、无新增第三方依赖、无新增写入路径（D1/D6）。
> 验收映射：规格 R1 派生优先→任务 2.x/5.1；R2 口径单一来源→1.1/1.2；R3 访问控制与审计→3.1/3.2；
> R4 不进指标导出→4.2；R5 小样本输出形态→4.1；R6 保留档归属→6.2（本版本不触发物化，仅验证未新增档）；
> R7 只读不影响主链路→2.5/3.3。

## 1. 口径契约单一来源（D3）

- [ ] 1.1 新增产品度量口径契约 fixture（活跃判定、日界与时区、窗口开闭规则、去重主体、跨通道归并、
  主动推送回应的窗口与触发来源分类、视图字段封闭集），并在文档中标注为口径唯一出处。
  验证：fixture 可被 JSON 解析且每个口径键有非空定义；仓库内不存在第二处同名口径定义（grep 交叉检查）。
  证据：`task-1.1-metric-contract.md`
- [ ] 1.2 新增口径契约测试：断言派生层与视图引用的口径键集合与 fixture 一致（缺失/多余即失败），
  并断言 fixture 中不存在内容类字段（消息正文、prompt、tool args、附件内容）。
  验证：`pytest -q -W error tests/` 中该测试文件全绿；人为删一个口径键时测试失败。
  证据：`task-1.2-contract-test.md`
- [ ] 1.3 时区与日界用例：以固定时钟构造跨日界与窗口起止整点的交互，断言唯一归属、不重计不漏计。
  验证：测试通过且断言覆盖「边界时刻计入左窗」的开闭规则；证据含实际使用的边界时间戳。
  证据：并入 `task-1.2-contract-test.md`

## 2. 派生查询层（D1/D6/D7）

- [ ] 2.1 实现账号活跃与回访派生（按时间窗给出逐账号绝对计数、最近活跃时间、主动发起与回应式两类拆分），
  走既有 tenant-bound 读路径与 bounded executor，只读事务。
  验证：真 PG 用例——同一账号跨两通道产生交互时账号级计数为 1、通道级各为 1；空窗返回空集不报错。
  证据：`task-2.1-active-derive.md`
- [ ] 2.2 实现激活漏斗派生（Token 兑换 → 首次用户入站 → 跨端绑定完成），每阶段返回账号集合与绝对计数，
  未转化账号可被直接列出。
  验证：真 PG 用例覆盖三种停留位置（仅兑换 / 有入站未绑定 / 全通过）各一次，输出可定位到具体账号。
  证据：`task-2.2-activation-funnel.md`
- [ ] 2.3 实现通道使用分布与功能采用面派生（通道消息/turn 计数、附件使用、persona 改动、记忆增长），
  字段集受 1.1 fixture 约束。
  验证：真 PG 用例断言各通道计数与手工 SQL 统计一致；响应字段集为 fixture 声明的封闭集，无额外键。
  证据：`task-2.3-adoption-derive.md`
- [ ] 2.4 实现主动推送回应派生（出站投递成功后同会话窗口内首个用户入站计为回应，窗口取 fixture 值），
  输出含分子分母绝对值，不输出达标/未达标判定。
  验证：真 PG 用例覆盖窗口内回应、窗口外入站、无入站三种情形；结果不含任何阈值判定字段。
  证据：`task-2.4-proactive-response.md`
- [ ] 2.5 实现请求侧护栏：时间窗必填或有默认有界窗口、结果规模有上限、无界明细请求被拒绝或收敛并回告实际窗口；
  派生失败/超时降级为「该度量不可用」并记录，不抛出到主链路。
  验证：测试断言无界请求返回实际使用的时间窗；注入查询异常时主链路 turn 处理结果不变（复用既有 turn 测试路径）。
  证据：`task-2.5-read-guards.md`

## 3. 管理端点与访问控制（D2/D5）

- [ ] 3.1 产品度量端点挂在既有管理面路由下，鉴权复用 admin principal；普通租户请求不返回任何其他账号的标识或度量值。
  验证：HTTP 用例——非 admin 401/403 且不返回度量字段；租户态请求响应中 grep 不到他人 account_id。
  证据：`task-3.1-admin-endpoints.md`
- [ ] 3.2 跨账号/跨 tenant 下钻与导出各写一条 `AdminAccessAuditEvent`（action 取既有 `drill_down` / `export`），
  事件经脱敏且不含内容原文。
  验证：两类操作后审计事件存在且字段齐（principal/target_kind/tenant_id/reason/at）；
  对含敏感文本的输入，审计序列化结果 grep 不到原文。
  证据：并入 `task-3.1-admin-endpoints.md`
- [ ] 3.3 确认产品度量不申请任何带身份维度的指标 label：静态扫描本 change 新增代码中对指标注册的使用，
  断言 label 集合 ⊆ `ALLOWED_METRIC_LABELS`；导出面中不存在可逐一定位到账号或单条消息的产品字段。
  验证：契约测试通过；`export_json` / `export_prometheus_text` 输出经断言不含 account 维度条目。
  证据：`task-3.3-no-identity-labels.md`

## 4. Dashboard 只读视图（D4/D5）

- [ ] 4.1 在现有 dashboard 增加只读产品区块：主表为「账号 × 时间窗」绝对事实列表，任何比率与其分子分母同处展示，
  视图标注当前账号总数 n；不引入前端二次计算口径。
  验证：前端构建通过；对同一时间窗，视图数值与端点响应逐字段一致（比对测试或截图证据 + 手工核对记录）。
  证据：`task-4.1-dashboard-view.md`
- [ ] 4.2 视图与批量导出引用同一口径来源：同一窗口内「活跃账号数」在视图与导出两处数值一致。
  验证：一致性测试通过（同一 fixture 时钟下比对两处输出）。证据：`task-4.2-consistency.md`

## 5. 缺口登记与不可产出边界（R1）

- [ ] 5.1 产出派生缺口清单：逐项列出本设计下看不见的交互（客户端已读/停留、未提交到服务端的 UI 动作、
  memory engine 选择动作），每项标注缺失原因与「不为其新增采集」的结论。
  验证：清单落盘且规格 R1 的三个 Scenario 各有对应说明；实现中不存在为补齐缺口的协议或采集改动
  （`git diff --stat` 中协议 fixture 与帧 schema 无变更）。
  证据：`task-5.1-blind-spots.md`
- [ ] 5.2 派生不出的度量在输出中呈现为「不可用」而非估值或占位数字。
  验证：测试断言该情形下响应字段为 null/缺失而非 0。证据：并入 `task-5.1-blind-spots.md`

## 6. 收口与全量验证

- [ ] 6.1 全量静态与测试验证：`pyright --level error`（project + tests 两配置）与
  `pytest -q -W error tests/` 通过，并记录与 main 基线的差异（应为零新增错误）。
  验证：命令 + 完整输出摘要落证据。证据：`task-6.1-full-verification.md`
- [ ] 6.2 确认零 schema 与零保留档变更：`alembic heads` 与 main 一致；`data-retention` 三档归属未被新增类别；
  连续执行任意度量查询后业务事实表行数不变。
  验证：命令输出 + 前后行数比对。证据：`task-6.2-no-schema-drift.md`
- [ ] 6.3 回滚演练：摘除产品视图路由与前端区块后服务正常启动、既有指标导出与对话链路不受影响；
  因无写入路径，确认回滚不需要数据清理。
  验证：本地实跑记录（启动日志 + 一次 turn 往返）。证据：`task-6.3-rollback.md`
- [ ] 6.4 更新本 change 状态：勾选已完成 checkbox，运行 `openspec status --change c16-product-analytics`
  与 `openspec validate c16-product-analytics --strict` 并记录输出；证据齐备后再走 sync/archive 流程。
  验证：`openspec validate --strict` 通过。证据：并入 `task-6.1-full-verification.md`
- [ ] 6.5 `openspec/PILOT_ROADMAP.md` 状态更新（仅在有证据时）：登记 C16 交付事实与派生基线报告的可读入口；
  `SCALING_ROADMAP.md` 不更新（本 change 不属于 scaling 编号序列，避免与 C6「指标驱动优化」的 scaling 语义混淆）。
  验证：文档 diff 与证据路径互相引用一致。证据：并入 `task-6.1-full-verification.md`
