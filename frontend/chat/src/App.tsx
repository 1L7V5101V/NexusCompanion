import { AssistantRuntimeProvider, ThreadPrimitive, ComposerPrimitive, MessagePrimitive } from "@assistant-ui/react";
import { ArrowUp } from "lucide-react";
import { useEffect, useState } from "react";
import { useChatRuntime } from "./store";
import type { ConnectionStatus } from "./connection";
import { fetchMe, logout, type AuthState } from "./auth";
import { LoginPanel } from "./LoginPanel";

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

function MessageBubble() {
  return (
    <MessagePrimitive.Root className="flex flex-col gap-1">
      <MessagePrimitive.Parts
        components={{
          Text: TextPart,
          Reasoning: ReasoningPart,
          tools: { Fallback: ToolPart },
        }}
      />
    </MessagePrimitive.Root>
  );
}

function TextPart({ text }: { text?: string }) {
  return <span className="whitespace-pre-wrap">{text}</span>;
}

function ReasoningPart({ text }: { text?: string }) {
  return (
    <div className="rounded-md border border-border bg-surface-2 px-3 py-2 text-xs text-muted whitespace-pre-wrap">
      {text}
    </div>
  );
}

function ToolPart({
  toolName,
  isError,
  result,
}: {
  toolName?: string;
  isError?: boolean;
  result?: unknown;
}) {
  return (
    <div className="rounded-md border border-border bg-surface-2 px-3 py-1.5 font-mono text-xs">
      <span className="text-subtle">tool</span>{" "}
      <span className={isError ? "text-danger" : "text-fg"}>{toolName}</span>
      {result ? <span className="text-subtle"> · {String(result)}</span> : null}
    </div>
  );
}

function UserMessage() {
  return (
    <div className="flex justify-end">
      <MessagePrimitive.Root className="max-w-[80%] rounded-xl bg-surface-3 px-4 py-2">
        <MessagePrimitive.Parts components={{ Text: TextPart }} />
      </MessagePrimitive.Root>
    </div>
  );
}

function ChatView({ onSignOut }: { onSignOut: () => void }) {
  // 只在已认证时挂载：未认证不会有 WS 连接（避免对 4401 反复重连）。
  const { runtime, status, isRunning } = useChatRuntime(onSignOut);

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

        <ThreadPrimitive.Viewport
          autoScroll
          className="flex-1 overflow-y-auto px-4 py-4"
        >
          <div className="mx-auto flex max-w-3xl flex-col gap-4">
            <ThreadPrimitive.Empty>
              <div className="py-24 text-center text-sm text-subtle">
                发送第一条消息开始对话
              </div>
            </ThreadPrimitive.Empty>
            <ThreadPrimitive.Messages
              components={{ UserMessage, AssistantMessage: MessageBubble }}
            />
          </div>
        </ThreadPrimitive.Viewport>

        <footer className="px-4 pb-6 pt-2">
          <div className="mx-auto max-w-3xl">
            <ComposerPrimitive.Root
              data-running={isRunning ? "true" : undefined}
              className="composer relative rounded-[28px] p-[2px]"
            >
              <div className="composer-halo" aria-hidden="true">
                <span className="composer-blob composer-blob-blue" />
                <span className="composer-blob composer-blob-violet" />
                <span className="composer-blob composer-blob-pink" />
                <span className="composer-blob composer-blob-amber" />
              </div>
              <div className="composer-ring" aria-hidden="true" />
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

export default function App() {
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

  return <ChatView onSignOut={() => setAuth({ phase: "anonymous" })} />;
}
