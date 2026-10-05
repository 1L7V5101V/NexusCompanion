# task-2.1 / 2.2 / 2.3 / 2.4 — onboarding 端点、门禁、锁定与模板管理（ADR-3）

日期：2026-10-04

## 变更（`bootstrap/chat_api.py`，仅 PG durable + auth 模式装配）

| 端点 | 语义 |
| --- | --- |
| `GET /api/persona/status` | `onboarding_required` + 启用模板目录摘要（仅 id/name） |
| `GET /api/persona/templates` | 启用模板正文；**已完成 onboarding → 404**（不再暴露） |
| `POST /api/persona/onboarding`（201） | template/custom 双路径；原子提交；重复 409；单块 >20k → 413；无修改端点（PATCH/PUT/DELETE → 404，UI 无编辑入口） |
| 用户内容面门禁 | uploads/media 依赖切到 `_require_onboarded`；WS 握手后校验 → close(4403, `persona_onboarding_required`)；未 onboarding → 403 + 机器可读码。sessions/messages 读路径维持原 session 门禁 |
| `GET /api/persona/source-breakdown` | ADR-6：仅 admin/debug（dev 模式可用；认证模式普通用户 404、admin principal 放行）；只出区块 metadata（label/chars/tokens/is_static/cache_hit），无正文无隐藏推理 |

dev 路径（durable/auth 缺一）：persona 端点不注册、门禁不生效——行为与 C9 之前
逐字一致（回归守卫 `test_dev_mode_persona_endpoints_absent`）。

实现备注：`_OnboardingBody`（pydantic）必须模块级定义——函数内定义 + PEP 563
延迟注解使 FastAPI 把 body 退化为 query 参数（422，实测发现）。

## 模板管理（task 2.4，CLI 方案）

`scripts/persona_admin.py`：`template-create`（--name + 三块文件/内联）、
`template-list [--all]`、`template-disable --id`。管理员应急/自动化入口，
与任务书"CLI 或最小 admin 端点二选一"一致（选 CLI；design 无需偏差注记）。

## 前端

- `frontend/chat/src/persona.ts`：status/templates/onboarding API（同 auth.ts 安全边界：
  HttpOnly Cookie，无本地存储）。
- `frontend/chat/src/OnboardingPanel.tsx`：模板预览采用 / 自由文本编辑两模式；
  提交成功 `onCompleted` → 聊天页；应用不提供再次进入 onboarding 的入口。
- `App.tsx`：认证后查 status（404 = dev 模式不拦截）→ onboarding_required 时渲染
  OnboardingPanel；提交后仅内存状态放行（无本地持久化）。
- `npm run build:chat` 通过（static/chat 产物已随提交入库）。

## 测试证据（tests/persona/test_endpoints.py + test_prompt_blocks.py）

- dev 模式端点不存在；status/onboarding/重复 409/模板 404 全链；
- 修改类（PATCH/PUT/DELETE/POST /api/persona/profile）全部 404；
- 未 onboarding uploads → 403 `persona_onboarding_required`；
- source-breakdown：普通用户 404 / dev 模式 200（只含 metadata）。
- stub auth runtime 走真实依赖链（cookie → validate_user_session → 身份派生）。
