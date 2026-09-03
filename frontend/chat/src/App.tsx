import { AssistantRuntimeProvider, ThreadPrimitive, ComposerPrimitive, MessagePrimitive } from "@assistant-ui/react";
import { useChatRuntime } from "./store";
import type { ConnectionStatus } from "./connection";

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

export default function App() {
  const { runtime, status } = useChatRuntime();

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="flex h-full flex-col bg-bg text-fg">
        <header className="flex items-center justify-between border-b border-border px-4 py-3">
          <h1 className="text-sm font-semibold tracking-tight">Nexus Chat</h1>
          <ConnectionBadge status={status} />
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

        <footer className="border-t border-border px-4 py-3">
          <div className="mx-auto max-w-3xl">
            <ComposerPrimitive.Root className="flex items-end gap-2 rounded-lg border border-border bg-surface px-3 py-2">
              <ComposerPrimitive.Input
                auto-focus
                rows={1}
                placeholder="输入消息…"
                className="max-h-40 flex-1 resize-none bg-transparent text-sm outline-none placeholder:text-subtle"
              />
              <ComposerPrimitive.Send
                className="rounded-md bg-accent px-3 py-1.5 text-sm text-accent-ink disabled:opacity-40"
              >
                发送
              </ComposerPrimitive.Send>
            </ComposerPrimitive.Root>
            <p className="mt-1 text-[10px] text-subtle">
              dev 模式：本地单用户，无认证
            </p>
          </div>
        </footer>
      </div>
    </AssistantRuntimeProvider>
  );
}
