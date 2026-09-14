# C5 recovery runbook 三路径演练记录（任务 5.3）

> 状态说明：本文件先把三条恢复路径的**可执行程序**落实到真实命令面
> （`main.py pilot-admin`、`bootstrap/auth/cli.py`、`crypto.py`、migration），
> 演练在本地 PG / 生产环境具备后按本文件执行并回填时间戳与结果。当前
> 未声称已完成实盘演练（不虚构验证证据）。

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

演练结论（待回填）：__执行人 / 时间 / 结果__

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

演练结论（待回填）：__执行人 / 时间 / 结果__

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

演练结论（待回填）：__执行人 / 时间 / 结果__

---

## 备注（运维边界）

- ADR-5：反代 / Cloudflare Tunnel 不得转发 `/api/admin/*`（allowlist 不是唯一
  防线；同机转发会表现为回环来源）。此边界在每次部署核对。
- pepper 文件名、路径不得写入普通日志/备份（`test_crypto.py::test_pepper_not_written_to_logs`
  已断言日志不含 pepper 值）。