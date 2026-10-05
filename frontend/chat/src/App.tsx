import { AssistantRuntimeProvider, ComposerPrimitive } from "@assistant-ui/react";
import { ArrowUp } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useChatRuntime, type ChatMessage } from "./store";
import { useMockChatRuntime } from "./mock";
import { AuroraCanvas } from "./AuroraCanvas";
import { ThinkingLine } from "./components/ThinkingLine";
import { ProcessEntry } from "./components/ProcessEntry";
import { StreamingMarkdown } from "./components/StreamingMarkdown";
import { useSmoothStream } from "./hooks/useSmoothStream";
import type { ConnectionStatus } from "./connection";
import { fetchMe, logout, type AuthState } from "./auth";
import { LoginPanel } from "./LoginPanel";
import { OnboardingPanel } from "./OnboardingPanel";
import { fetchPersonaStatus } from "./persona";
import { PulseBackground } from "./PulseBackground";
import type { PulseCoreState } from "./pulsecore/PulseCorePipeline";

const MOCK_MODE = new URLSearchParams(window.location.search).get("mock") === "1";

type ChatBundle = {
  runtime: React.ComponentProps<typeof AssistantRuntimeProvider>["runtime"];
  status: ConnectionStatus;
  isRunning: boolean;
  messages: readonly ChatMessage[];
  awaitingFirstToken: boolean;
};

function ConnectionBadge({ status }: { status: ConnectionStatus }) {
  const label =
    status === "online" ? "在线" : status === "connecting" ? "连接中" : "离线";
  const dotClass =
    status === "online"
      ? "bg-success"
      : status === "connecting"
        ? "bg-warning animate-pulse"
        : "bg-danger";
  return (
    <div className="flex items-center gap-2 px-1">
      <span className={`h-2 w-2 rounded-full ${dotClass}`} />
      <span className="text-xs text-muted">{label}</span>
    </div>
  );
}

function UserMessageView({ message }: { message: ChatMessage }) {
  const text = message.parts
    .map((part) => (part.kind === "text" ? part.text : ""))
    .join("");
  return (
    <div className="flex justify-end">
      <div className="bubble-user">
        <span className="whitespace-pre-wrap text-sm">{text}</span>
      </div>
    </div>
  );
}

/** 正文 part：useSmoothStream 匀速放出 → 气泡内 StreamingMarkdown 渲染。 */
function StreamingTextPart({ text, streaming }: { text: string; streaming: boolean }) {
  const shown = useSmoothStream(text);
  return (
    <div className="bubble-assistant">
      <StreamingMarkdown text={shown} streaming={streaming} />
    </div>
  );
}

/**
 * 助手消息三态：
 * - 思考/工具进行中：一行流光状态文字（无气泡），词库随末位 part 类型切换
 * - 正文起步（<20 字符）：流光行切到 writing 词，随后淡出让位
 * - 正文：左对齐气泡；过程信息折叠为一行「已思考 · 调用了工具」文字入口
 */
function AssistantMessageView({ message }: { message: ChatMessage }) {
  const parts = message.parts;
  let textIdx = -1;
  parts.forEach((p, i) => {
    if (p.kind === "text") textIdx = i;
  });
  type TextPart = Extract<ChatMessage["parts"][number], { kind: "text" }>;
  const textPart = textIdx >= 0 ? (parts[textIdx] as TextPart) : null;
  const processParts = parts.filter((p) => p.kind !== "text");
  const lastProcess = processParts[processParts.length - 1];
  const lastTool = lastProcess?.kind === "tool" ? lastProcess : null;

  // 正文起步过渡：目标文本 <20 字符时流光行切到 writing 词，暂不出气泡
  const writingPhase =
    message.status === "running" && !!textPart && textPart.text.length < 20;
  const thinkingLine = message.status === "running" && (!textPart || writingPhase);

  // 流光行淡出让位：正文可见后延迟卸载（transition 走完再摘除）
  const [lineVisible, setLineVisible] = useState(thinkingLine);
  useEffect(() => {
    if (thinkingLine) {
      setLineVisible(true);
      return;
    }
    const timer = setTimeout(() => setLineVisible(false), 320);
    return () => clearTimeout(timer);
  }, [thinkingLine]);

  const bodyVisible = !!textPart && !writingPhase;
  const lineCategory: "thinking" | "tool_running" | "writing" = writingPhase
    ? "writing"
    : lastProcess?.kind === "tool"
      ? "tool_running"
      : "thinking";

  return (
    <div className="flex flex-col gap-2">
      {lineVisible ? (
        <ThinkingLine
          category={lineCategory}
          toolName={lastTool?.toolName}
          hide={!thinkingLine}
        />
      ) : null}
      {bodyVisible ? (
        <StreamingTextPart text={textPart.text} streaming={message.status === "running"} />
      ) : null}
      {!bodyVisible && message.status === "error" && message.error ? (
        <div className="msg-error">请求失败：{message.error}</div>
      ) : null}
      {processParts.length > 0 && bodyVisible ? (
        <ProcessEntry parts={processParts} />
      ) : null}
    </div>
  );
}

/**
 * AI 状态 → PulseCore 背景状态：
 * - 最近一条消息失败 → error（直到下一次发送）
 * - 等待首 token → thinking；正文流式中 → streaming
 * - 其余 → idle
 */
function deriveAiState(
  messages: readonly ChatMessage[],
  isRunning: boolean,
  awaitingFirstToken: boolean,
): PulseCoreState {
  const last = messages[messages.length - 1];
  if (last?.status === "error") return "error";
  if (isRunning || awaitingFirstToken) {
    return awaitingFirstToken ? "thinking" : "streaming";
  }
  return "idle";
}

function ChatRender({ chat, onSignOut }: { chat: ChatBundle; onSignOut: () => void }) {
  const { runtime, status, messages, isRunning, awaitingFirstToken } = chat;
  const aiState = deriveAiState(messages, isRunning, awaitingFirstToken);
  const rootRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const composerWrapRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const [showJump, setShowJump] = useState(false);

  const handleScroll = useCallback(() => {
    const el = viewportRef.current;
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    atBottomRef.current = dist < 80;
    setShowJump(!atBottomRef.current);
  }, []);

  // 输入栏实测高度 → --composer-h：同步滚动容器底部内边距、虚化层高度、
  // 回到底部按钮位置；多行增高时若处于贴底状态则保持贴底不跳动
  useEffect(() => {
    const wrap = composerWrapRef.current;
    const root = rootRef.current;
    if (!wrap || !root) return;
    const apply = () => {
      root.style.setProperty(
        "--composer-h",
        `${Math.round(wrap.getBoundingClientRect().height)}px`,
      );
      if (atBottomRef.current) {
        const vp = viewportRef.current;
        if (vp) vp.scrollTop = vp.scrollHeight;
      }
    };
    const observer = new ResizeObserver(apply);
    observer.observe(wrap);
    apply();
    return () => observer.disconnect();
  }, []);

  // 内容增长（流式输出/折叠动画）时，用户停留在底部附近（<80px）则贴底
  useEffect(() => {
    const el = viewportRef.current;
    const content = contentRef.current;
    if (!el || !content) return;
    const observer = new ResizeObserver(() => {
      if (atBottomRef.current) el.scrollTop = el.scrollHeight;
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, []);

  const jumpToBottom = useCallback(() => {
    const el = viewportRef.current;
    if (!el) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    el.scrollTo({ top: el.scrollHeight, behavior: reduced ? "auto" : "smooth" });
    atBottomRef.current = true;
    setShowJump(false);
  }, []);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      {/* app-root：overflow 用 clip（见 CSS）——裁掉流光画布向下出界的部分，
          同时不产生滚动容器（hidden 会让画布外伸计入 scrollHeight，
          焦点滚动把整个界面推上去）。背景为 PulseCore 脉冲核心（fixed 层）。 */}
      <div ref={rootRef} className="app-root relative flex h-full flex-col text-fg">
        <PulseBackground state={aiState} />
        <header className="relative z-10 flex items-center justify-between border-b border-border px-4 py-3">
          <h1 className="text-sm font-semibold tracking-tight">Nexus Chat</h1>
          <div className="flex items-center gap-3">
            <ConnectionBadge status={status} />
            <button
              type="button"
              onClick={async () => {
                await logout();
                onSignOut();
              }}
              className="text-xs text-muted underline-offset-2 hover:underline"
            >
              退出
            </button>
          </div>
        </header>

        <div className="relative min-h-0 flex-1">
        <div
          ref={viewportRef}
          onScroll={handleScroll}
          className="thread-scroll h-full overflow-y-auto px-4 pt-4"
          style={{
            paddingBottom:
              "calc(var(--composer-h) + var(--composer-safe-bottom) + var(--composer-gap-bottom) + var(--thread-pad-bottom))",
          }}
        >
            <div ref={contentRef} className="mx-auto flex max-w-3xl flex-col gap-5">
              {messages.length === 0 ? (
                <div className="py-24 text-center text-sm text-subtle">
                  发送第一条消息开始对话
                </div>
              ) : (
                messages.map((message) =>
                  message.role === "user" ? (
                    <UserMessageView key={message.id} message={message} />
                  ) : (
                    <AssistantMessageView key={message.id} message={message} />
                  ),
                )
              )}
              {awaitingFirstToken ? <ThinkingLine category="thinking" /> : null}
            </div>
          </div>

          {/* 渐进虚化层：纯视觉、不拦截点击；两层递进 blur + 一层渐隐着色 */}
          <div className="thread-fade" aria-hidden="true">
            <div className="thread-fade-blur thread-fade-blur-far" />
            <div className="thread-fade-blur thread-fade-blur-near" />
            <div className="thread-fade-tint" />
          </div>

          {showJump ? (
            <button type="button" className="scroll-jump" onClick={jumpToBottom}>
              回到底部 ↓
            </button>
          ) : null}
        </div>

        {/* 悬浮输入栏：外层全透明、不拦截点击（两侧空白可滚动下方内容） */}
        <div className="composer-float">
          <div ref={composerWrapRef} className="pointer-events-auto mx-auto max-w-3xl">
            <ComposerPrimitive.Root
              data-running={isRunning ? "true" : undefined}
              className="composer relative rounded-[28px] p-[2px]"
            >
              <AuroraCanvas active={isRunning} />
              <div className="composer-body relative rounded-[26px] ring-1 ring-inset ring-border">
                <ComposerPrimitive.Input
                  auto-focus
                  rows={1}
                  placeholder="输入消息…"
                  className="max-h-40 w-full resize-none bg-transparent px-5 pt-4 text-sm outline-none placeholder:text-subtle"
                />
                <div className="flex items-center justify-end px-3.5 pb-3 pt-2">
                  <ComposerPrimitive.Send
                    aria-label="发送"
                    className="flex h-10 w-10 items-center justify-center rounded-full bg-[#4285f4] text-white transition-colors hover:bg-[#5b95f6] disabled:opacity-40"
                  >
                    <ArrowUp size={18} strokeWidth={2.5} />
                  </ComposerPrimitive.Send>
                </div>
              </div>
            </ComposerPrimitive.Root>
          </div>
        </div>
      </div>
    </AssistantRuntimeProvider>
  );
}

function Splash({ label }: { label: string }) {
  return (
    <div className="flex h-full items-center justify-center bg-bg text-sm text-muted">
      {label}
    </div>
  );
}

/** 真实链路：登录 → ChatRender（useChatRuntime 仅在已认证时挂载，避免 4401 重连）。 */
function RealApp() {
  const [auth, setAuth] = useState<AuthState>({ phase: "checking" });
  const [onboardingRequired, setOnboardingRequired] = useState(false);

  // 刷新/重开浏览器：凭 HttpOnly Cookie 确认会话（不读取任何本地存储）。
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const user = await fetchMe();
        if (!cancelled) {
          setAuth(user ? { phase: "authenticated", user } : { phase: "anonymous" });
        }
      } catch {
        if (!cancelled) setAuth({ phase: "anonymous" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // 登录后询问 onboarding 状态（C9；端点不存在 = dev 模式 → 不拦截，行为不变）。
  useEffect(() => {
    if (auth.phase !== "authenticated") return;
    let cancelled = false;
    void (async () => {
      try {
        const status = await fetchPersonaStatus();
        if (!cancelled) setOnboardingRequired(status.onboarding_required);
      } catch {
        if (!cancelled) setOnboardingRequired(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [auth.phase]);

  if (auth.phase === "checking") {
    return <Splash label="正在确认登录状态…" />;
  }
  if (auth.phase === "anonymous") {
    return (
      <>
        <PulseBackground state="idle" />
        <LoginPanel
          onAuthenticated={(user) =>
            setAuth(user ? { phase: "authenticated", user } : { phase: "anonymous" })
          }
        />
      </>
    );
  }
  if (onboardingRequired) {
    return (
      <>
        <PulseBackground state="idle" />
        <OnboardingPanel onCompleted={() => setOnboardingRequired(false)} />
      </>
    );
  }
  return (
    <RealChatView
      onSignOut={() => setAuth({ phase: "anonymous" })}
    />
  );
}

function RealChatView({ onSignOut }: { onSignOut: () => void }) {
  const chat = useChatRuntime(onSignOut);
  return <ChatRender chat={chat} onSignOut={onSignOut} />;
}

/** ?mock=1：本地演示，不建立任何真实连接。 */
function MockChatView() {
  const chat = useMockChatRuntime();
  return <ChatRender chat={chat} onSignOut={() => {}} />;
}

export default function App() {
  return MOCK_MODE ? <MockChatView /> : <RealApp />;
}
