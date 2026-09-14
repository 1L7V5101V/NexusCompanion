# C5 recovery runbook 三路径演练记录（任务 5.3）

> 状态说明：三条恢复路径已按本文件在**部署态 canary 环境**逐条实跑并回填结论
> （A / B / C 三路径全 PASS）。原始转录见
> [`runbook-drill-transcript.txt`](runbook-drill-transcript.txt)，驱动脚本见
> [`runbook-drill-script.sh`](runbook-drill-script.sh)。
>
> 执行人为 AI（Claude Code），环境为 canary（`nexus-c5-canary` + `nexus-pg-c5`，
> 与生产容器/库隔离），**不是**人工在生产环境执行。生产侧的真人验收仍属 PR
> 「实际验证」人工填写项，本节不代填、不勾选。

对应 design ADR-1（pepper 轮换 = 全部 digest 失效，维护窗口）、ADR-5
（admin 网络边界进入 runbook）、§5.9.3（明文只显示一次）。

---

## 前提（三路径共用）

- 命令入口：`python main.py pilot-admin <子命令>`，`config.toml` 指向控制面
  PG，按 §5.9.3 在受信主机交互式 TTY 执行。
- pepper 位置：env `NEXUS_AUTH_PEPPER` 或 `<workspace>/secrets/auth_pepper`。
  **pepper 与数据库备份必须对应同一代次**：切换 pepper 源/恢复数据库后，全部
  既有 digest 会失效，需按路径恢复。
- 时间参数冻结：session idle 7d / absolute 30d；admin idle 30min / absolute
  12h（会话创建时固化到行）。

---

## 路径 A：lost-token（recovery token 丢失，未外泄）

场景：唯一收悉过的 `nad_` 明文丢失/未保存，admin 网页登录不可用；无泄漏面。

程序：
1. 在受信主机 TTY 执行 `python main.py pilot-admin rotate-recovery-token --force-local`。
   - 当前 recovery token 校验被跳过（`--force-local`），仅为受信主机 TTY 门禁
     （`cli.py:170` `isatty()` 校验）；立即在 TTY 回显新 `nad_` 一次并保存。
   - `revision` +1、`rotated_at` 更新；admin 浏览器会话不受影响（旋转与撤销
     为独立操作，§10 DECIDED）。
2. 验证：`python main.py pilot-admin status` 显示 `revision` 增长；用新 token
   `POST /api/admin/auth/exchange` 成功（本机来源）。
3. 收尾：新 token 使用后进入企业 secret 保存；旧明文彻底丢弃。

演练结论：执行人 = AI（Claude Code）；时间 = 2026-09-14；结果 = **PASS**。
实跑记录（transcript 路径 A）：`rotate-recovery-token --force-local` 在
`script` 分配的容器 pty 上通过 `isatty()` 门禁并捕获新明文
（`len=47`，前缀 `nad_`，CLI 原始输出随即删除）；`status` 显示
`revision` 12 → 13；以新令牌 `POST /api/admin/auth/exchange` 返回 `200`
且下发了 admin 会话 cookie。收尾：新明文仅留存于容器 `/tmp/drill/T1`
（演练后已删除，见文末「收尾与残留」）。

---

## 路径 B：suspected-leak（recovery token 疑似泄露）

场景：token 可能已被外部获取，需要**立刻**让泄露的明文失效，并切断潜在会话。

程序：
1. **立即**在受信主机 TTY 执行
   `python main.py pilot-admin rotate-recovery-token --force-local`（新 digest
   替换；旧明文即使泄露也已不可兑换）。
2. 若怀疑浏览器会话已被劫持（不只 token 泄露）：
   `python main.py pilot-admin revoke-sessions --all`——撤销全部 admin 会话，
   recovery token 不变。
   - 与第 1 步的先后关系：rotate 用 `--force-local` 不需要旧 token，先 rotate
     更安全；session 撤销独立执行。
3. 核对审计：`admin_audit_events` 应有
   `admin.rotate_recovery`（detail.force_local=true）与 `admin.revoke_sessions`
   两条事件（由 service 层写入，无明文）。
4. 兜底（可选）：临时关闭管理入口 `python main.py pilot-admin disable`，再按
   需要 `enable`（disable 同时撤销全部 admin session，enable 不复活）。
5. 验证：旧 token 兑换 `POST /api/admin/auth/exchange` 必须 401（统一文案，
   不泄露原因，ADR-3）。

演练结论：执行人 = AI（Claude Code）；时间 = 2026-09-14；结果 = **PASS**。
实跑记录（transcript 路径 B）：B1 再次 `rotate-recovery-token --force-local`
后，**轮换前建立的 admin 会话仍可访问** `/api/admin/test-accounts`（200），
实证 §10 DECIDED 的「轮换与撤销为独立操作」；B2 `revoke-sessions --all`
撤销 2 个会话，同一会话随即 401；B3 审计核对
`admin.rotate_recovery {"force_local": true}` 与
`admin.revoke_sessions {"revoked": 2}` 均落库且无明文；B4 `disable` → `enable`
后管理入口重开；B5 **已泄露的旧明文 T1 兑换 401**（统一文案，不泄露原因）。

---

## 路径 C：database-restore（数据库恢复 / pepper 与 DB 代次失配）

场景：PG 控制面库从备份恢复，或 pepper 文件被重置；all digest 判定失败
（`401 invalid credentials` / `authentication required`）。恢复后 admin
principal 与全部会话/token digest 都基于旧 pepper 计算。

程序：
1. **先确认 pepper 代次**：检查
   `NEXUS_AUTH_PEPPER` / `<workspace>/secrets/auth_pepper` 是否为备份对应
   代次（备份时应将 pepper 一并备份；pepper 属 secret，备份要加密且与 DB
   快照同代，见 ADR-1）。
   - 同代 → 直接验证恢复成功：admin 会话/token 贯通。
   - 失配（pepper 已丢失）→ 走第 2 步。
2. pepper 丢失时全部凭据失效，重建 admin：
   `python main.py pilot-admin bootstrap`（仅当 `admin_credentials` 单行缺失；
   若旧行存在，先手动清行——运维操作，备份后执行）。
   - 用 `pilot-admin bootstrap` 后立即可用；`revoke-sessions --all` 兜底清掉
     任何恢复后的残留 admin 会话。
3. 全部既有用户 token/session 因 pepper 变更失效：属于**声明式轮换**（ADR-1
   `rotation`），需重新走邀请签发/兑换流程；向受影响账号说明并重新发邀请
   （`/api/admin/test-accounts` 创建→签发）。
4. 验证：`status` 正常返回；抽查一个账号重新签发 + 兑换成功；`access_tokens`
   /`auth_sessions` 只含 64-hex digest（`test_migration.py` 的约束断言语义）。
5. 收尾：把「pepper 必须与 DB 备份同代次」写回备份操作清单。

演练结论：执行人 = AI（Claude Code）；时间 = 2026-09-14；结果 = **PASS**。
实跑记录（transcript 路径 C）：C0 备份 `admin_credentials` 单行（revision 14）
并备份 pepper（sha256 前 16 位 `a4f13bbcf352c841`）；制造代次失配（写入新代次
pepper `c66e3633897b3c4f` + 重启服务）后，旧 recovery token、旧 admin 会话、
既有用户邀请 token **三者全部 401**（含未兑换的邀请 token，证明失效面覆盖
全部 digest 而非仅活跃会话）；清行后 `pilot-admin bootstrap` 生成新令牌 T3
→ 兑换 200，并重新签发邀请走通「重发邀请即恢复」；最后把 pepper 与
`admin_credentials` 行**同时**恢复到备份（同代次）→ 原 admin 会话 ck2 与
原令牌 T2 重新贯通（200），而失配期新建的 ck3 仍 401——该反向对照证明
「贯通」确由 pepper 代次匹配带来，而非会话未过期。

---

## 演练环境与执行偏差

- **环境**：canary 容器 `nexus-c5-canary`（真实进程 + 真实控制面库
  `nexus-pg-c5`，经 SSH tunnel 访问）+ 真实 Cookie/CSRF/Origin/回环门禁。
  与生产容器 `NexusCompanion` 完全隔离（独立容器、独立网络、独立库，
  生产 config 无 `[auth]` 段），演练全程未触碰生产。
- **入口**：CLI 经 `script -qec "docker exec -it …"` 提供 pty 以通过
  `isatty()` 门禁；HTTP 探针在容器内打 `http://127.0.0.1:2236`，
  `Origin` 取 `config.toml [auth] origin_allowlist` 中的
  `http://127.0.0.1:2237`。
- **偏差 1（明文处置）**：CLI 明文 token 只写入宿主机临时文件并立即提取进
  容器 `/tmp/drill/`，原始 CLI 输出随即删除；脚本 stdout 只打印长度/前缀
  等非敏感事实，全程未把 secrets 输出到输出流。
- **偏差 2（路径 C 用户面探针下沉到服务层）**：canary 未启用 chat 通道，
  用户面 `/api/auth/*` **未挂载**（HTTP 404）。故路径 C 中用户邀请 token 的
  兑换断言改走服务层同一实现路径
  （`CredentialRepository.consume_token_for_session`，不经 HTTP transport），
  并在 transcript 中标注为 `service user-exchange`；该端点的 transport 契约
  已由 PG 集成套件 `test_http_contract.py` 覆盖，此处不重复声称 HTTP 验证。
- **偏差 3（两次服务重启）**：pepper 由 `PepperProvider` 按 runtime 缓存在
  内存，改文件后必须重启进程才生效（CLI 每次新建 runtime 故读文件即最新）。
  路径 C 因此在「制造失配」与「恢复」各重启一次，并以端口探针等待就绪。
- **transcript 中的 `^@`**：由 `script` + `docker exec -it` 的 pty 包装产生
  的字面两字符，非内容、非凭据；transcript 按原始捕获保留未做删改。

## 收尾与残留

- 演练结束时状态：`admin: enabled`，`revision: 14`（= C0 备份行 revision，
  即恢复到演练前状态），`active_sessions: 3`。
- digest-only 复核（部署态库内）：`access_tokens` 64-hex 12/12、
  `auth_sessions` 64-hex 23/23、`admin_audit_events` 明文前缀
  （`nxt_`/`nad_`/`ns_`）命中 **0**。
- 演练产生的明文（`T1`/`T2`/`T3`、三个 admin cookie、四个用户邀请 token）与
  pepper 备份副本均只存在于服务器临时路径，演练后**已全部删除**；因此
  canary 当前的 recovery token 明文不再留存，需要时按路径 A
  `--force-local` 重新轮换获取。
- 未做：把本演练写成可重复的 CI 任务（现为一次性脚本，脚本已随证据归档）。

---

## 备注（运维边界）

- ADR-5：反代 / Cloudflare Tunnel 不得转发 `/api/admin/*`（allowlist 不是唯一
  防线；同机转发会表现为回环来源）。此边界在每次部署核对。
- pepper 文件名、路径不得写入普通日志/备份（`test_crypto.py::test_pepper_not_written_to_logs`
  已断言日志不含 pepper 值）。