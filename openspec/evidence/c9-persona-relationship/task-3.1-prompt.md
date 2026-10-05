# task-3.1 / 3.2 / 3.3 / 3.4 / 4.1 — prompt 四层、单写者、生效时机与 config 边界

日期：2026-10-04

## prompt 四层组装（ADR-4）

穿线链（per-turn，无共享可变态）：

```
run_turn（tenant lane 内，msg.tenant_id 已知）
  → persona_resolver(tenant_id) 解析一次 → PersonaSnapshot（frozen）
  → PromptRenderInput.persona_snapshot
  → PromptRenderCtx / ContextRequest.persona_snapshot
  → PromptAssembler.assemble(persona_snapshot=…)
  → _build_system_prompt_result(persona_snapshot=…)
  → TurnContext.persona_snapshot
  → IdentityPromptBlock（身份+规则） / SelfModelPromptBlock（关系状态）
```

- 快照存在：身份块 = 快照 identity + personality_rules（沿用单体段落形状）；
  self 块 = 快照 relationship_state。
- 快照缺失（onboarding 未完成 / dev/SQLite）：逐字回退现有
  `build_agent_static_identity_prompt` / `ctx.memory.read_self()` 语义
  （`test_identity_block_falls_back_without_snapshot` /
  `test_self_block_falls_back_to_file_without_snapshot`）。
- **缓存防线**：`IdentityPromptBlock.cache_signature` 在有快照时返回 None——
  static 缓存 scope 是进程级 workspace 路径，跨 tenant 缓存会泄漏（fail-closed）。
- 注入实现：`DefaultReasoner.bind_persona_resolver`（bootstrap 在 PG durable
  装配后绑定）；解析异常 fail-open 回退单体语义（不阻断 turn）。
- ChannelPolicy 层：现有 channel policy 块不动（本就 per-channel）。
- RuntimeInvariant 层：现有代码维护规则块不动。

## 单写者与生效时机（ADR-5）

- `bootstrap/persona.py::PgRelationshipIO.write`：memory_items self 行
  UPDATE（无行则补种）+ `persona_audit_events(relationship_update)` **同事务**；
  detail 只记 chars 不复制正文。串行由调用方保证（optimizer per-tenant lock +
  maintenance lane）。
- `MemoryOptimizer` 增 `relationship_io` 注入 + tenant_id 穿线
  （`optimize(tenant_id) → _optimize(tenant_id) → _update_self(pending, tenant_id)`）；
  PG durable 模式读写走 PG seam，None 时单体文件语义不变。
- 生效时机：快照是 frozen dataclass，turn 组装期解析一次并持有——进行中 turn
  用原 snapshot（`test_snapshot_is_immutable_per_turn`），新值随下一轮 turn 的
  重新解析注入。

## config 边界与恢复（task 4.1，ADR-7）

- 多租户运行时的 tenant 值解析只读 PG（`resolve_persona_snapshot`）；
  `config.toml [agent.persona]` 仅经 `apply_persona_config` 服务 dev 单体路径
  （无快照回退），PG 模式已 onboarding tenant 不受其影响。
- 验证：`tests/persona/test_endpoints.py::test_config_seed_does_not_override_onboarded_tenant`
  （seed 修改后 PG 快照逐字不变）；重启恢复 = PG 当前值（快照每次从 PG 解析，
  `test_relationship_io_write_updates_and_audits` 读写一致）。

## 测试证据

- prompt 块单元（无 PG）：`tests/persona/test_prompt_blocks.py` 7 passed——
  快照优先/回退/缓存禁用/两租户互斥渲染/frozen 语义；
- 隔离（3.2）：`test_profiles_are_tenant_isolated`（PG 双 tenant）+
  `test_two_tenants_render_differently`（prompt 层互斥，含主对话组装路径；
  Proactive/Drift 走同一 ContextBuilder 组装缝 → 同一快照判据）；
- breakdown 权限（3.4）：`test_source_breakdown_admin_debug_only`。
