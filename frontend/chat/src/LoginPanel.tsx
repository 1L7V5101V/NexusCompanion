/**
 * WebChat 登录 / 注册入口（invite-code-tenant-registration）。
 *
 * 交互（极简 + 点击展开）：
 * - 收起态：屏幕正中只有「Nexus」字标 + 毛玻璃胶囊「登录」按钮；
 * - 点击胶囊在**同一元素**上形变为毛玻璃表单面板（宽/高/圆角过渡，
 *   毛玻璃参数恒定不变——只动画尺寸与透明，避免闪烁）；
 * - 点击面板外部或 Esc 收回为胶囊（已输入内容不清空）；
 * - 注册入口为面板内小号文字链接，同一面板内平滑切换（高度过渡 + 淡入）。
 *
 * 认证逻辑与约束不变：两个表单（登录 / 邀请码注册）的提交、校验、
 * 错误处理、autocomplete 与 onAuthenticated 回调保持原样。
 * 凭据只经 same-origin POST 提交，成功后由服务端下发 HttpOnly Cookie；
 * 本组件不持久化 Token/密码，也不写入 URL。错误文案统一（不区分
 * 邮箱不存在 / 密码错误 / 账号被禁用，§5.9.3 不泄露存在性）。
 */

import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type FormEvent } from "react";
import { login, registerTenant, AuthError, type AuthUser } from "./auth";

type Mode = "login" | "register";

/** 逐项淡入上移的序号（CSS 变量 --i，配合 stagger 延迟） */
const step = (i: number): CSSProperties => ({ "--i": i } as CSSProperties);

export function LoginPanel({ onAuthenticated }: { onAuthenticated: (user: AuthUser | null) => void }) {
  const [expanded, setExpanded] = useState(false);
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
  // 每次报错自增：让提示元素重挂载，抖动动画能重复触发
  const [errorTick, setErrorTick] = useState(0);

  const bodyRef = useRef<HTMLDivElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const inviteRef = useRef<HTMLInputElement>(null);
  // 展开时面板的目标高度 = 表单内容实测高度（收起态也保持测量，形变无需等待）
  const [panelH, setPanelH] = useState(240);

  // 内容高度跟随（登录/注册切换、错误提示出现 → 外壳高度平滑过渡）
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const apply = () => setPanelH(el.offsetHeight);
    apply();
    const observer = new ResizeObserver(apply);
    observer.observe(el);
    return () => observer.disconnect();
  }, [mode]);

  // 展开后自动聚焦首个字段（略延迟，避开形变首帧与移动端键盘抢焦点）
  useEffect(() => {
    if (!expanded) return;
    const timer = window.setTimeout(() => {
      (mode === "register" ? inviteRef.current : emailRef.current)?.focus();
    }, 300);
    return () => window.clearTimeout(timer);
  }, [expanded, mode]);

  // Esc 收回（内容保留）
  useEffect(() => {
    if (!expanded) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExpanded(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [expanded]);

  function expand() {
    setExpanded(true);
    setError(null);
  }

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
      setErrorTick((n) => n + 1);
    } finally {
      setSubmitting(false);
    }
  }

  const onLogin = (e: FormEvent<HTMLFormElement>) => submitForm(e, () => login(loginEmail, loginPassword));
  const onRegister = (e: FormEvent<HTMLFormElement>) =>
    submitForm(e, () => registerTenant(inviteToken, regEmail, regPassword));

  const loginReady = loginEmail.trim().length > 0 && loginPassword.length > 0;
  const regReady = inviteToken.trim().length > 0 && regEmail.trim().length > 0 && regPassword.length > 0;
  const ready = mode === "login" ? loginReady : regReady;

  return (
    // 点击面板外部收回：舞台上任意空白处触发，面板自身阻止冒泡
    <div className="login-stage" onClick={() => setExpanded(false)}>
      <div className="flex flex-col items-center gap-5">
        <div className="login-brand">Nexus</div>

        <div
          className={`login-card login-shell${expanded ? " is-open" : ""}`}
          style={{ "--login-h": `${panelH}px` } as CSSProperties}
          onClick={(event) => event.stopPropagation()}
        >
          {/* 收起态：胶囊按钮（展开时淡出并移出 Tab 序） */}
          <button
            type="button"
            className="login-cta"
            aria-label="展开登录表单"
            aria-expanded={expanded}
            tabIndex={expanded ? -1 : 0}
            onClick={expand}
          >
            登录
          </button>

          {/* 展开态：表单内容（收起时 visibility:hidden，保留在布局里供测量） */}
          <div ref={bodyRef} className="login-body">
            {mode === "login" ? (
              <form key="login" className="login-swap flex flex-col gap-3.5" aria-label="登录" onSubmit={onLogin}>
                <input
                  ref={emailRef}
                  id="login-email"
                  name="email"
                  type="email"
                  autoComplete="email"
                  placeholder="邮箱"
                  value={loginEmail}
                  onChange={(event) => setLoginEmail(event.target.value)}
                  className="login-field login-anim"
                  style={step(0)}
                />
                <input
                  id="login-password"
                  name="password"
                  type="password"
                  autoComplete="current-password"
                  placeholder="密码"
                  value={loginPassword}
                  onChange={(event) => setLoginPassword(event.target.value)}
                  className="login-field login-anim"
                  style={step(1)}
                />
                {error ? (
                  <p key={errorTick} className="login-error" role="alert">
                    {error}
                  </p>
                ) : null}
                <button
                  type="submit"
                  disabled={submitting || !ready}
                  aria-busy={submitting}
                  className="login-submit login-anim"
                  style={step(2)}
                >
                  {submitting ? "登录中…" : "登录"}
                </button>
                <div className="login-anim text-center" style={step(3)}>
                  <button type="button" className="login-link" onClick={() => switchMode("register")}>
                    用邀请码注册
                  </button>
                </div>
              </form>
            ) : (
              <form key="register" className="login-swap flex flex-col gap-3.5" aria-label="注册" onSubmit={onRegister}>
                <input
                  ref={inviteRef}
                  id="reg-invite"
                  name="invite_token"
                  type="password"
                  autoComplete="off"
                  placeholder="邀请码"
                  value={inviteToken}
                  onChange={(event) => setInviteToken(event.target.value)}
                  className="login-field login-anim"
                  style={step(0)}
                />
                <input
                  id="reg-email"
                  name="email"
                  type="email"
                  autoComplete="off"
                  placeholder="邮箱"
                  value={regEmail}
                  onChange={(event) => setRegEmail(event.target.value)}
                  className="login-field login-anim"
                  style={step(1)}
                />
                <input
                  id="reg-password"
                  name="password"
                  type="password"
                  autoComplete="new-password"
                  placeholder="设置密码（至少 8 位）"
                  value={regPassword}
                  onChange={(event) => setRegPassword(event.target.value)}
                  className="login-field login-anim"
                  style={step(2)}
                />
                {error ? (
                  <p key={errorTick} className="login-error" role="alert">
                    {error}
                  </p>
                ) : null}
                <button
                  type="submit"
                  disabled={submitting || !ready}
                  aria-busy={submitting}
                  className="login-submit login-anim"
                  style={step(3)}
                >
                  {submitting ? "注册中…" : "注册并进入"}
                </button>
                <div className="login-anim text-center" style={step(4)}>
                  <button type="button" className="login-link" onClick={() => switchMode("login")}>
                    返回登录
                  </button>
                </div>
              </form>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
