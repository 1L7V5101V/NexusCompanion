/**
 * WebChat 登录 / 注册入口（invite-code-tenant-registration）。
 *
 * 两个表单：
 * - 注册：邀请码 + 邮箱 + 密码（一次性消费租户邀请码，自动开通并登录）
 * - 登录：邮箱 + 密码
 *
 * 凭据只经 same-origin POST 提交，成功后由服务端下发 HttpOnly Cookie；
 * 本组件不持久化 Token/密码，也不写入 URL。错误文案统一（不区分
 * 邮箱不存在 / 密码错误 / 账号被禁用，§5.9.3 不泄露存在性）。
 */

import { useState, type FormEvent } from "react";
import { login, registerTenant, AuthError, type AuthUser } from "./auth";

type Mode = "login" | "register";

export function LoginPanel({ onAuthenticated }: { onAuthenticated: (user: AuthUser | null) => void }) {
  const [mode, setMode] = useState<Mode>("login");
  // 登录表单
  const [loginEmail, setLoginEmail] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  // 注册表单
  const [inviteToken, setInviteToken] = useState("");
  const [regEmail, setRegEmail] = useState("");
  const [regPassword, setRegPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function switchMode(next: Mode) {
    setMode(next);
    setError(null);
  }

  async function submitForm(event: FormEvent<HTMLFormElement>, fn: () => Promise<AuthUser | null>) {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const user = await fn();
      // 成功后清除表单（凭据不保留在前端状态里）。
      setInviteToken("");
      setRegEmail("");
      setRegPassword("");
      setLoginEmail("");
      setLoginPassword("");
      onAuthenticated(user);
    } catch (err) {
      setError(err instanceof AuthError ? err.message : "请求失败，请稍后重试。");
    } finally {
      setSubmitting(false);
    }
  }

  const onLogin = (e: FormEvent<HTMLFormElement>) =>
    submitForm(e, () => login(loginEmail, loginPassword));
  const onRegister = (e: FormEvent<HTMLFormElement>) =>
    submitForm(e, () => registerTenant(inviteToken, regEmail, regPassword));

  const loginReady = loginEmail.trim().length > 0 && loginPassword.length > 0;
  const regReady = inviteToken.trim().length > 0 && regEmail.trim().length > 0 && regPassword.length > 0;

  return (
    <div className="flex h-full items-center justify-center bg-bg px-4 text-fg">
      <div className="w-full max-w-sm rounded-xl border border-border bg-surface px-5 py-6">
        <h1 className="text-base font-semibold tracking-tight">Nexus Chat</h1>
        <p className="mt-1 text-xs text-muted">
          {mode === "login"
            ? "使用邮箱和密码登录。会话由浏览器安全 Cookie 维持。"
            : "凭邀请码注册新租户：邮箱 + 密码 + 邀请码（一次性）。"}
        </p>

        <div className="mt-4 grid grid-cols-2 gap-1 rounded-md border border-border bg-surface-2 p-1 text-xs">
          <button
            type="button"
            onClick={() => switchMode("login")}
            className={`rounded px-2 py-1.5 transition-colors ${
              mode === "login" ? "bg-accent text-accent-ink" : "text-muted hover:text-fg"
            }`}
          >
            登录
          </button>
          <button
            type="button"
            onClick={() => switchMode("register")}
            className={`rounded px-2 py-1.5 transition-colors ${
              mode === "register" ? "bg-accent text-accent-ink" : "text-muted hover:text-fg"
            }`}
          >
            注册
          </button>
        </div>

        {mode === "login" ? (
          <form onSubmit={onLogin} className="mt-4 space-y-3">
            <label className="block text-xs text-muted" htmlFor="login-email">
              邮箱
            </label>
            <input
              id="login-email"
              name="email"
              type="email"
              autoComplete="email"
              autoFocus
              value={loginEmail}
              onChange={(event) => setLoginEmail(event.target.value)}
              placeholder="you@example.com"
              className="mt-1 w-full rounded-md border border-border bg-surface-2 px-3 py-2 text-sm outline-none placeholder:text-subtle focus:border-accent"
            />
            <label className="block text-xs text-muted" htmlFor="login-password">
              密码
            </label>
            <input
              id="login-password"
              name="password"
              type="password"
              autoComplete="current-password"
              value={loginPassword}
              onChange={(event) => setLoginPassword(event.target.value)}
              placeholder="••••••••"
              className="mt-1 w-full rounded-md border border-border bg-surface-2 px-3 py-2 text-sm outline-none placeholder:text-subtle focus:border-accent"
            />
            {error ? <p className="text-xs text-danger">{error}</p> : null}
            <button
              type="submit"
              disabled={submitting || !loginReady}
              className="w-full rounded-md bg-accent px-3 py-2 text-sm text-accent-ink disabled:opacity-40"
            >
              {submitting ? "登录中…" : "登录"}
            </button>
          </form>
        ) : (
          <form onSubmit={onRegister} className="mt-4 space-y-3">
            <label className="block text-xs text-muted" htmlFor="reg-invite">
              邀请码
            </label>
            <input
              id="reg-invite"
              name="invite_token"
              type="password"
              autoComplete="off"
              value={inviteToken}
              onChange={(event) => setInviteToken(event.target.value)}
              placeholder="粘贴邀请码…"
              className="mt-1 w-full rounded-md border border-border bg-surface-2 px-3 py-2 font-mono text-sm outline-none placeholder:text-subtle focus:border-accent"
            />
            <label className="block text-xs text-muted" htmlFor="reg-email">
              邮箱
            </label>
            <input
              id="reg-email"
              name="email"
              type="email"
              autoComplete="off"
              value={regEmail}
              onChange={(event) => setRegEmail(event.target.value)}
              placeholder="you@example.com"
              className="mt-1 w-full rounded-md border border-border bg-surface-2 px-3 py-2 text-sm outline-none placeholder:text-subtle focus:border-accent"
            />
            <label className="block text-xs text-muted" htmlFor="reg-password">
              设置密码
            </label>
            <input
              id="reg-password"
              name="password"
              type="password"
              autoComplete="new-password"
              value={regPassword}
              onChange={(event) => setRegPassword(event.target.value)}
              placeholder="至少 8 位"
              className="mt-1 w-full rounded-md border border-border bg-surface-2 px-3 py-2 text-sm outline-none placeholder:text-subtle focus:border-accent"
            />
            {error ? <p className="text-xs text-danger">{error}</p> : null}
            <button
              type="submit"
              disabled={submitting || !regReady}
              className="w-full rounded-md bg-accent px-3 py-2 text-sm text-accent-ink disabled:opacity-40"
            >
              {submitting ? "注册中…" : "注册并进入"}
            </button>
          </form>
        )}

        {mode === "register" ? (
          <p className="mt-3 text-center text-[11px] text-subtle">
            已有账号？{" "}
            <button type="button" onClick={() => switchMode("login")} className="text-accent hover:underline">
              去登录
            </button>
          </p>
        ) : (
          <p className="mt-3 text-center text-[11px] text-subtle">
            还没有账号？{" "}
            <button type="button" onClick={() => switchMode("register")} className="text-accent hover:underline">
              用邀请码注册
            </button>
          </p>
        )}
      </div>
    </div>
  );
}