import type { PageResult } from "./types";

let activeTenant = "";

/** 设置 dashboard 当前操作的 tenant_id（空 = 由后端回落到配置 owner tenant）。 */
export function setActiveTenant(tenant: string): void {
  activeTenant = tenant.trim();
}

export function getActiveTenant(): string {
  return activeTenant;
}

/** 给 dashboard API 路径集中注入 tenant_id（保留已存在的查询参数，不覆盖显式值）。 */
function withTenant(url: string): string {
  if (!activeTenant || !url.startsWith("/api/dashboard/")) {
    return url;
  }
  const [path, query] = url.split("?", 2);
  const params = new URLSearchParams(query ?? "");
  if (!params.has("tenant_id")) {
    params.set("tenant_id", activeTenant);
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

export async function api<T = unknown>(url: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(withTenant(url), {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers ?? {}),
    },
    ...options,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new Error(payload.detail || `请求失败: ${response.status}`);
  }
  if (response.status === 204) {
    return null as T;
  }
  return response.json() as Promise<T>;
}

export function pageCount(total: number, pageSize: number): number {
  return Math.max(1, Math.ceil(total / pageSize));
}

export function asPageResult<T>(payload: PageResult<T>): PageResult<T> {
  return {
    items: payload.items ?? [],
    total: payload.total ?? 0,
    page: payload.page,
    page_size: payload.page_size,
  };
}
