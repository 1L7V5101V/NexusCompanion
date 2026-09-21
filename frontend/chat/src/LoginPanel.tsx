/**
 * 邀请码登录入口（C5 设计 ADR-5：用户只输入一次 Token）。
 *
 * 只把 Token 提交给 `/api/auth/exchange`，成功后由服务端下发 HttpOnly Cookie；
 * 本组件不持久化 Token，也不把它写入 URL。
 */

import { useState, type FormEvent } from "react";
import { exchangeInvitation, ExchangeError, type AuthUser } from "./auth";

export function LoginPanel({ onAuthenticated }: { onAuthenticated: (user: AuthUser | null) => void }) {
  const [token, setToken] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = token.trim();
    if (!value || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const user = await exchangeInvitation(value);
      setToken("");
      onAuthenticated(user);
    } catch (err) {
      setError(err instanceof ExchangeError ? err.message : "登录失败，请稍后重试。");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex h-full items-center justify-center bg-bg px-4 text-fg">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm rounded-xl border border-border bg-surface px-5 py-6"
      >
        <h1 className="text-base font-semibold tracking-tight">Nexus Chat</h1>
        <p className="mt-1 text-xs text-muted">
          请输入邀请码。邀请码只能使用一次，登录状态由浏览器安全 Cookie 维持。
        </p>

        <label className="mt-5 block text-xs text-muted" htmlFor="invite-token">
          邀请码
        </label>
        <input
          id="invite-token"
          name="token"
          type="password"
          autoComplete="off"
          autoFocus
          value={token}
          onChange={(event) => setToken(event.target.value)}
          placeholder="粘贴邀请码…"
          className="mt-1 w-full rounded-md border border-border bg-surface-2 px-3 py-2 font-mono text-sm outline-none placeholder:text-subtle"
        />

        {error ? <p className="mt-2 text-xs text-danger">{error}</p> : null}

        <button
          type="submit"
          disabled={submitting || token.trim().length === 0}
          className="mt-4 w-full rounded-md bg-accent px-3 py-2 text-sm text-accent-ink disabled:opacity-40"
        >
          {submitting ? "登录中…" : "登录"}
        </button>
      </form>
    </div>
  );
}
