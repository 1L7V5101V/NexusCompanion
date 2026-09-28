import { AssistantRuntimeProvider, ComposerPrimitive } from "@assistant-ui/react";
import { ArrowUp } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useChatRuntime, type ChatMessage } from "./store";
import { useMockChatRuntime } from "./mock";
import { AuroraCanvas } from "./AuroraCanvas";
import { ThinkingDots } from "./components/ThinkingDots";
import { ThinkingBlock } from "./components/ThinkingBlock";
import { ToolCallCard } from "./components/ToolCallCard";
import { StreamingMarkdown } from "./components/StreamingMarkdown";
import { useSmoothStream } from "./hooks/useSmoothStream";
import type { ConnectionStatus } from "./connection";
import { fetchMe, logout, type AuthState } from "./auth";
import { LoginPanel } from "./LoginPanel";

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
      <div className="max-w-[80%] rounded-2xl bg-surface-3 px-4 py-2">
        <span className="whitespace-pre-wrap text-sm">{text}</span>
      </div>
    </div>
  );
}

/** 正文 part：useSmoothStream 匀速放出 → StreamingMarkdown 渲染。 */
function StreamingTextPart({ text, streaming }: { text: string; streaming: boolean }) {
  const shown = useSmoothStream(text);
  return <StreamingMarkdown text={shown} streaming={streaming} />;
}

function AssistantMessageView({ message }: { message: ChatMessage }) {
  const parts = message.parts;
  const last = parts.length - 1;
  return (
    <div className="flex flex-col gap-2">
      {parts.map((part, i) => {
        if (part.kind === "reasoning") {
          return (
            <ThinkingBlock
              key={i}
              text={part.text}
              active={message.status === "running" && i === last}
            />
          );
        }
        if (part.kind === "tool") {
          return (
            <ToolCallCard key={i} name={part.toolName} state={part.state} result={part.result} />
          );
        }
        return (
          <StreamingTextPart
            key={i}
            text={part.text}
            streaming={message.status === "running" && i === last}
          />
        );
      })}
      {message.status === "error" && message.error ? (
        <div className="msg-error">请求失败：{message.error}</div>
      ) : null}
    </div>
  );
}

function ChatRender({ chat, onSignOut }: { chat: ChatBundle; onSignOut: () => void }) {
  const { runtime, status, messages, isRunning, awaitingFirstToken } = chat;
  const viewportRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const [showJump, setShowJump] = useState(false);

  const handleScroll = useCallback(() => {
    const el = viewportRef.current;
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    atBottomRef.current = dist < 80;
    setShowJump(!atBottomRef.current);
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
      <div className="flex h-full flex-col bg-bg text-fg">
        <header className="flex items-center justify-between border-b border-border px-4 py-3">
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
            className="h-full overflow-y-auto px-4 py-4"
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
              <ThinkingDots active={awaitingFirstToken} />
            </div>
          </div>
          {showJump ? (
            <button type="button" className="scroll-jump" onClick={jumpToBottom}>
              回到底部 ↓
            </button>
          ) : null}
        </div>

        <footer className="px-4 pb-6 pt-2">
          <div className="mx-auto max-w-3xl">
            <ComposerPrimitive.Root
              data-running={isRunning ? "true" : undefined}
              className="composer relative rounded-[28px] p-[2px]"
            >
              <AuroraCanvas active={isRunning} />
              <div className="relative rounded-[26px] bg-surface-3 ring-1 ring-inset ring-border">
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
        </footer>
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

  if (auth.phase === "checking") {
    return <Splash label="正在确认登录状态…" />;
  }
  if (auth.phase === "anonymous") {
    return (
      <LoginPanel
        onAuthenticated={(user) =>
          setAuth(user ? { phase: "authenticated", user } : { phase: "anonymous" })
        }
      />
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
