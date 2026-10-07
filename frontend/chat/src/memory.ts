/**
 * Memory engine selector API（C14）。与 auth.ts/persona.ts 同一安全边界：
 * 凭 HttpOnly Cookie（credentials: same-origin），不读写任何本地存储。
 * 目录/权限/readiness 全部由服务端判定，客户端只提交 engine_id 设置请求。
 */

export type MemoryEngineInfo = {
  engine_id: string;
  display_name: string;
  description: string;
  binding_policy: string;
  ready: boolean;
  selectable: boolean;
  active: boolean;
};

export type MemoryEnginesStatus = {
  engines: MemoryEngineInfo[];
  active_engine: string;
  tenant_policy_revision: number;
};

export class MemoryEnginesUnavailableError extends Error {
  constructor() {
    super("memory engines unavailable");
    this.name = "MemoryEnginesUnavailableError";
  }
}

export async function fetchMemoryEngines(): Promise<MemoryEnginesStatus> {
  const resp = await fetch("/api/memory/engines", {
    credentials: "same-origin",
    headers: { accept: "application/json" },
  });
  // dev 模式 / 服务未装配：端点不存在（404）→ 功能不可用，调用方隐藏选择器
  if (resp.status === 404) throw new MemoryEnginesUnavailableError();
  if (!resp.ok) throw new Error(`memory engines status failed: ${resp.status}`);
  return (await resp.json()) as MemoryEnginesStatus;
}

export async function setActiveEngine(
  engineId: string,
): Promise<{ active_engine: string; tenant_policy_revision: number }> {
  const resp = await fetch("/api/memory/engines/active", {
    method: "PUT",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ engine_id: engineId }),
  });
  if (!resp.ok) {
    let detail = `memory engine selection failed: ${resp.status}`;
    try {
      const body = (await resp.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // 保持默认 detail
    }
    throw new Error(detail);
  }
  return (await resp.json()) as {
    active_engine: string;
    tenant_policy_revision: number;
  };
}
