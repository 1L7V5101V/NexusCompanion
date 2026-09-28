import {
  useExternalStoreRuntime,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChatConnection, type ConnectionStatus, type HistoryMessage } from "./connection";
import type {
  MessageDeltaFrame,
  ToolCompletedFrame,
  ToolStartedFrame,
  TurnCompletedFrame,
  TurnFailedFrame,
} from "./protocol";

/** 内部 content part 模型（与 assistant-ui 的 part 语义一一对应）。 */
type Part =
  | { kind: "text"; text: string }
  | { kind: "reasoning"; text: string }
  | {
      kind: "tool";
      toolCallId: string;
      toolName: string;
      state: "running" | "complete" | "error";
      result?: string;
    };

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  parts: Part[];
  status: "running" | "complete" | "error";
  /** turn.failed 的错误信息（渲染层展示用）。 */
  error?: string;
};

function toAssistantStatus(status: ChatMessage["status"]): ThreadMessageLike["status"] {
  if (status === "running") return { type: "running" };
  if (status === "error") {
    return { type: "incomplete", reason: "error" };
  }
  return { type: "complete", reason: "stop" };
}

/** snapshot 消息 → assistant-ui 消息（渲染与 runtime 共用）。 */
export function toThreadMessageLike(message: ChatMessage): ThreadMessageLike {
  return {
    id: message.id,
    role: message.role,
    content: toContent(message.parts),
    ...(message.role === "assistant"
      ? { status: toAssistantStatus(message.status) }
      : {}),
  };
}

function toContent(parts: readonly Part[]): ThreadMessageLike["content"] {
  return parts.map((part): Extract<NonNullable<ThreadMessageLike["content"]>[number], { type: string }> => {
    if (part.kind === "text") {
      return { type: "text", text: part.text };
    }
    if (part.kind === "reasoning") {
      return { type: "reasoning", text: part.text };
    }
    return {
      type: "tool-call",
      toolCallId: part.toolCallId,
      toolName: part.toolName,
      argsText: "",
      result: part.result,
      isError: part.state === "error",
    };
  });
}

/**
 * ChatStore 把 ServerFrame 流映射为消息列表。框架无关；assistant-ui 耦合
 * 只发生在 useChatRuntime（useExternalStoreRuntime）。
 */
export class ChatStore {
  private messages: ChatMessage[] = [];
  private running = false;
  private listeners = new Set<() => void>();

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getSnapshot = (): { messages: readonly ChatMessage[]; isRunning: boolean } => {
    return { messages: this.messages, isRunning: this.running };
  };

  private notify(): void {
    for (const listener of [...this.listeners]) listener();
  }

  private findMessage(id: string): ChatMessage | undefined {
    return this.messages.find((m) => m.id === id);
  }

  private commit(next: ChatMessage[]): void {
    this.messages = next;
    this.notify();
  }

  private replaceMessage(id: string, next: ChatMessage): void {
    this.commit(this.messages.map((m) => (m.id === id ? next : m)));
  }

  handleHistory(messages: HistoryMessage[]): void {
    this.running = false;
    this.messages = messages.map((m) => ({
      id: `history-${m.seq}`,
      role: m.role,
      parts: [{ kind: "text", text: m.content }],
      status: "complete",
    }));
    this.notify();
  }

  handleFrame(frame: { type: string } & Record<string, unknown>): void {
    switch (frame.type) {
      case "message.delta":
        this.applyDelta(frame as unknown as MessageDeltaFrame);
        return;
      case "tool.started":
        this.applyToolStarted(frame as unknown as ToolStartedFrame);
        return;
      case "tool.completed":
        this.applyToolCompleted(frame as unknown as ToolCompletedFrame);
        return;
      case "turn.completed":
        this.applyTurnCompleted(frame as unknown as TurnCompletedFrame);
        return;
      case "turn.failed":
        this.applyTurnFailed(frame as unknown as TurnFailedFrame);
        return;
      default:
        return;
    }
  }

  /** 用户从 composer 发送：乐观插入 user 消息并经 connection 发出。 */
  appendUserMessage(send: (content: string) => string, text: string): void {
    const clientMessageId = send(text);
    this.running = true;
    this.commit([
      ...this.messages,
      {
        id: clientMessageId,
        role: "user",
        parts: [{ kind: "text", text }],
        status: "complete",
      },
    ]);
  }

  private ensureRunningAssistant(turnId: string): ChatMessage {
    if (turnId) {
      const existing = this.findMessage(turnId);
      if (existing) return existing;
    }
    if (this.currentAssistantId) {
      const existing = this.findMessage(this.currentAssistantId);
      if (existing) return existing;
    }
    const id = turnId || `assistant-${Date.now()}`;
    const message: ChatMessage = {
      id,
      role: "assistant",
      parts: [],
      status: "running",
    };
    this.currentAssistantId = id;
    this.commit([...this.messages, message]);
    return message;
  }

  private currentAssistantId: string | null = null;

  private applyDelta(frame: MessageDeltaFrame): void {
    const message = this.ensureRunningAssistant(frame.turn_id);
    if (!frame.thinking_delta && !frame.content_delta) return;
    // 按到达顺序追加：思考接在末位 reasoning 之后（或新开），正文同理——
    // 思考 → 工具 → 再思考 → 正文 的交错结构不合并。
    const parts = [...message.parts];
    if (frame.thinking_delta) {
      const last = parts[parts.length - 1];
      if (last && last.kind === "reasoning") {
        parts[parts.length - 1] = { ...last, text: last.text + frame.thinking_delta };
      } else {
        parts.push({ kind: "reasoning", text: frame.thinking_delta });
      }
    }
    if (frame.content_delta) {
      const last = parts[parts.length - 1];
      if (last && last.kind === "text") {
        parts[parts.length - 1] = { ...last, text: last.text + frame.content_delta };
      } else {
        parts.push({ kind: "text", text: frame.content_delta });
      }
    }
    this.replaceMessage(message.id, { ...message, parts, status: "running" });
  }

  private applyToolStarted(frame: ToolStartedFrame): void {
    const message = this.ensureRunningAssistant(frame.turn_id);
    const parts = [...message.parts];
    const index = parts.findIndex(
      (part) => part.kind === "tool" && part.toolCallId === frame.call_id,
    );
    const part: Part = {
      kind: "tool",
      toolCallId: frame.call_id,
      toolName: frame.tool_name,
      state: "running",
    };
    if (index >= 0) parts[index] = part;
    else parts.push(part);
    this.replaceMessage(message.id, { ...message, parts });
  }

  private applyToolCompleted(frame: ToolCompletedFrame): void {
    const message = this.ensureRunningAssistant(frame.turn_id);
    const parts = [...message.parts];
    const index = parts.findIndex(
      (part) => part.kind === "tool" && part.toolCallId === frame.call_id,
    );
    const part: Part = {
      kind: "tool",
      toolCallId: frame.call_id,
      toolName: frame.tool_name,
      state: frame.status === "error" ? "error" : "complete",
      result: frame.result_preview,
    };
    if (index >= 0) parts[index] = part;
    else parts.push(part);
    this.replaceMessage(message.id, { ...message, parts });
  }

  private applyTurnCompleted(frame: TurnCompletedFrame): void {
    const existingId =
      (frame.turn_id ? this.findMessage(frame.turn_id)?.id : undefined) ??
      (this.currentAssistantId ? this.findMessage(this.currentAssistantId)?.id : undefined) ??
      frame.turn_id ??
      `assistant-${Date.now()}`;
    this.currentAssistantId = null;
    this.running = false;
    // Update isRunning before notifying subscribers so the composer is re-enabled.
    const existing = this.findMessage(existingId);
    let parts: Part[] = existing ? [...existing.parts] : [];
    // 终态是 source of truth：仅当与流式累积不一致时才重建（保留交错结构，
    // 正文重建到末位、思考重建到首位；一致则原样保留，避免破坏交错）。
    const textConcat = parts
      .filter((p) => p.kind === "text")
      .map((p) => p.text)
      .join("");
    const thinkConcat = parts
      .filter((p) => p.kind === "reasoning")
      .map((p) => p.text)
      .join("");
    if ((frame.content ?? "") !== textConcat) {
      parts = parts.filter((p) => p.kind !== "text");
      if (frame.content) parts.push({ kind: "text", text: frame.content });
    }
    if ((frame.thinking ?? "") !== thinkConcat) {
      parts = parts.filter((p) => p.kind !== "reasoning");
      if (frame.thinking) parts.unshift({ kind: "reasoning", text: frame.thinking });
    }
    const finalMessage: ChatMessage = {
      id: existingId,
      role: "assistant",
      parts,
      status: "complete",
    };
    if (existing) {
      this.replaceMessage(existingId, finalMessage);
    } else {
      this.commit([...this.messages, finalMessage]);
    }
  }

  private applyTurnFailed(frame: TurnFailedFrame): void {
    const existing = frame.turn_id
      ? this.findMessage(frame.turn_id)
      : this.currentAssistantId
        ? this.findMessage(this.currentAssistantId)
        : undefined;
    const id = existing?.id ?? frame.turn_id ?? `assistant-error-${Date.now()}`;
    const finalMessage: ChatMessage = {
      id,
      role: "assistant",
      parts: existing ? existing.parts : [],
      status: "error",
      error: frame.error,
    };
    // Keep the snapshot consistent with the terminal assistant message.
    this.currentAssistantId = null;
    this.running = false;
    if (existing) {
      this.replaceMessage(existing.id, finalMessage);
    } else {
      this.commit([...this.messages, finalMessage]);
    }
  }
}

export function useChatRuntime(onUnauthorized?: () => void): {
  runtime: ReturnType<typeof useExternalStoreRuntime>;
  status: ConnectionStatus;
  isRunning: boolean;
  messages: readonly ChatMessage[];
  /** 已发出用户消息但尚未收到任何 assistant 内容（渲染跳动省略号）。 */
  awaitingFirstToken: boolean;
} {
  const store = useMemo(() => new ChatStore(), []);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  // 回调放 ref：避免调用方每次渲染传入新函数时重建连接（会造成 WS 重连循环）。
  const unauthorizedRef = useRef(onUnauthorized);
  unauthorizedRef.current = onUnauthorized;
  const connection = useMemo(
    () =>
      new ChatConnection({
        onStatus: setStatus,
        onFrame: (frame) => store.handleFrame(frame),
        onHistory: (messages) => store.handleHistory(messages),
        onUnauthorized: () => unauthorizedRef.current?.(),
      }),
    [store],
  );

  useEffect(() => {
    connection.connect();
    return () => connection.dispose();
  }, [connection]);

  const [snapshot, setSnapshot] = useState(() => store.getSnapshot());
  useEffect(
    () => store.subscribe(() => setSnapshot(store.getSnapshot())),
    [store],
  );

  const onNew = useCallback(
    async (message: { content: readonly { type: string; text?: string }[] }) => {
      const text = message.content
        .map((part) => (part.type === "text" ? (part.text ?? "") : ""))
        .join("");
      store.appendUserMessage((content) => connection.send(content), text);
    },
    [connection, store],
  );

  const onCancel = useCallback(async () => {
    // dev v0 不实现服务端取消（协作式取消是后续阶段能力）。
  }, []);

  const runtime = useExternalStoreRuntime<ThreadMessageLike>({
    messages: snapshot.messages.map(toThreadMessageLike),
    isRunning: snapshot.isRunning,
    onNew,
    onCancel,
    convertMessage: (message) => message,
  });

  const lastMessage = snapshot.messages[snapshot.messages.length - 1];
  const awaitingFirstToken =
    snapshot.isRunning && (!lastMessage || lastMessage.role === "user");

  return {
    runtime,
    status,
    isRunning: snapshot.isRunning,
    messages: snapshot.messages as readonly ChatMessage[],
    awaitingFirstToken,
  };
}
