import {
  useExternalStoreRuntime,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import { useCallback, useEffect, useMemo, useState } from "react";
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

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  parts: Part[];
  status: "running" | "complete" | "error";
};

function toAssistantStatus(status: ChatMessage["status"]): ThreadMessageLike["status"] {
  if (status === "running") return { type: "running" };
  if (status === "error") {
    return { type: "incomplete", reason: "error" };
  }
  return { type: "complete", reason: "stop" };
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
    let reasoning = "";
    let text = "";
    for (const part of message.parts) {
      if (part.kind === "reasoning") reasoning += part.text;
      else if (part.kind === "text") text += part.text;
    }
    reasoning += frame.thinking_delta;
    text += frame.content_delta;

    const parts: Part[] = [];
    if (reasoning) parts.push({ kind: "reasoning", text: reasoning });
    if (text) parts.push({ kind: "text", text });
    for (const part of message.parts) {
      if (part.kind === "tool") parts.push(part);
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
    // turn.completed 是终态 source of truth：覆盖 delta 累积文本。
    const finalMessage: ChatMessage = {
      id: existingId,
      role: "assistant",
      parts: [
        ...(frame.thinking ? [{ kind: "reasoning" as const, text: frame.thinking }] : []),
        { kind: "text", text: frame.content },
      ],
      status: "complete",
    };
    if (this.findMessage(existingId)) {
      this.replaceMessage(existingId, finalMessage);
    } else {
      this.commit([...this.messages, finalMessage]);
    }
    this.currentAssistantId = null;
    this.running = false;
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
      parts: existing ? existing.parts : [{ kind: "text", text: frame.error }],
      status: "error",
    };
    if (existing) {
      this.replaceMessage(existing.id, finalMessage);
    } else {
      this.commit([...this.messages, finalMessage]);
    }
    this.currentAssistantId = null;
    this.running = false;
  }
}

export function useChatRuntime(): {
  runtime: ReturnType<typeof useExternalStoreRuntime>;
  status: ConnectionStatus;
} {
  const store = useMemo(() => new ChatStore(), []);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const connection = useMemo(
    () =>
      new ChatConnection({
        onStatus: setStatus,
        onFrame: (frame) => store.handleFrame(frame),
        onHistory: (messages) => store.handleHistory(messages),
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
    messages: snapshot.messages.map((message): ThreadMessageLike => ({
      id: message.id,
      role: message.role,
      content: toContent(message.parts),
      status: toAssistantStatus(message.status),
    })),
    isRunning: snapshot.isRunning,
    onNew,
    onCancel,
    convertMessage: (message) => message,
  });

  return { runtime, status };
}
