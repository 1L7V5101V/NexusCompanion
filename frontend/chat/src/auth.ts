/**
 * 用户面认证（C5 design ADR-5）。
 *
 * 安全边界：本模块 SHALL NOT 把邀请 Token 或 session 写入 localStorage /
 * sessionStorage / URL / 日志。凭据只以服务端下发的 HttpOnly Cookie 形式存在，
 * 前端仅以 `credentials: "same-origin"` 自动携带；页面刷新后凭 Cookie 重新确认。
 */

export type AuthUser = {
  account_id: string;
  display_name: string;
  status: string;
};

export type AuthState =
  | { phase: "checking" }
  | { phase: "anonymous" }
  | { phase: "authenticated"; user: AuthUser };

/** 邀请 Token 兑换失败（401 = 无效/已用过；403 = 来源不在 allowlist）。 */
export class ExchangeError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(
      status === 401
        ? "邀请码无效，或已被使用过。"
        : status === 403
          ? "来源校验未通过，请联系管理员确认访问地址。"
          : "登录失败，请稍后重试。",
    );
    this.name = "ExchangeError";
    this.status = status;
  }
}

/** 邮箱密码登录/注册失败（401 = 凭据错误；403 = 来源不在 allowlist）。 */
export class AuthError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(
      status === 401
        ? "邮箱或密码不正确。"
        : status === 403
          ? "来源校验未通过，请联系管理员确认访问地址。"
          : "请求失败，请稍后重试。",
    );
    this.name = "AuthError";
    this.status = status;
  }
}

/** 读当前会话；未登录（401/403）返回 null。 */
export async function fetchMe(): Promise<AuthUser | null> {
  const resp = await fetch("/api/auth/me", {
    credentials: "same-origin",
    headers: { accept: "application/json" },
  });
  if (resp.status === 401 || resp.status === 403) return null;
  if (!resp.ok) throw new Error(`me failed: ${resp.status}`);
  return (await resp.json()) as AuthUser;
}

/**
 * 一次性兑换邀请 Token。成功后服务端 Set-Cookie（HttpOnly），
 * 前端不保存任何凭据；随后重新读 /me 以获得展示名。
 */
export async function exchangeInvitation(token: string): Promise<AuthUser | null> {
  const resp = await fetch("/api/auth/exchange", {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ token: token.trim() }),
  });
  if (!resp.ok) throw new ExchangeError(resp.status);
  return fetchMe();
}

/**
 * 邮箱+密码登录。凭据只经请求体发送 → 服务端 Set-Cookie（HttpOnly）；
 * 前端不持久化任何内容。
 */
export async function login(email: string, password: string): Promise<AuthUser | null> {
  const resp = await fetch("/api/auth/login", {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email: email.trim(), password }),
  });
  if (!resp.ok) throw new AuthError(resp.status);
  return fetchMe();
}

/**
 * 租户自助注册（邀请码 + 邮箱 + 密码）。成功后同样由服务端建立会话。
 */
export async function registerTenant(
  inviteToken: string,
  email: string,
  password: string,
): Promise<AuthUser | null> {
  const resp = await fetch("/api/auth/register", {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      invite_token: inviteToken.trim(),
      email: email.trim(),
      password,
    }),
  });
  if (!resp.ok) throw new AuthError(resp.status);
  return fetchMe();
}

/** 注销当前会话。mutation 需 session-bound CSRF（`x-csrf-token`）+ Origin。 */
export async function logout(): Promise<void> {
  let csrf = "";
  try {
    const csrfResp = await fetch("/api/auth/csrf", { credentials: "same-origin" });
    if (csrfResp.ok) {
      const body = (await csrfResp.json()) as { csrf_token?: string };
      csrf = body.csrf_token ?? "";
    }
  } catch {
    // 取不到 CSRF 时仍尝试注销；服务端会以 403 拒绝，不影响本地状态收敛。
  }
  await fetch("/api/auth/logout", {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json", "x-csrf-token": csrf },
  });
}
