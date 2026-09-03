import {
  CLOSE_OVERLOAD,
  PROTOCOL_VERSION,
  parseServerFrame,
  type ClientFrame,
  type ServerFrame,
} from "./protocol";

export type ConnectionStatus = "connecting" | "online" | "offline";

export type HistoryMessage = {
  seq: number;
  role: "user" | "assistant";
  content: string;
  thinking: string | null;
  createdAt: string | null;
};

export type ConnectionEvents = {
  onStatus: (status: ConnectionStatus) => void;
  onFrame: (frame: ServerFrame) => void;
  onHistory: (messages: HistoryMessage[]) => void;
};

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 8000;
const HISTORY_PAGE_SIZE = 200;

/**
 * ChatConnection 是协议唯一接缝：WS 连接、hello 握手、client_message_id
 * 生成、重连退避 + replay 补拉、REST 历史重建。上层 store 只消费
 * ServerFrame 回调，不知道传输细节。
 */
export class ChatConnection {
  private readonly events: ConnectionEvents;
  private ws: WebSocket | null = null;
  private status: ConnectionStatus = "offline";
  private lastSeq = 0;
  private reconnectAttempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private disposed = false;
  /** 已发送、尚未收到 message.accepted 的 client_message_id 集合。 */
  private readonly pendingIds = new Set<string>();

  constructor(events: ConnectionEvents) {
    this.events = events;
  }

  connect(): void {
    if (this.disposed || this.ws) return;
    this.setStatus("connecting");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws = ws;

    ws.onopen = () => {
      // hello 在服务端 accept 后立即到达，不需要客户端先发帧。
    };
    ws.onmessage = (event) => {
      const frame =
        typeof event.data === "string" ? parseServerFrame(event.data) : null;
      if (!frame) return;
      this.handleFrame(frame);
    };
    ws.onclose = (event) => {
      this.ws = null;
      if (event.code === CLOSE_OVERLOAD) {
        // 服务端过载断开：立即重连（客户端视角与普通断线相同）。
      }
      this.scheduleReconnect();
    };
    ws.onerror = () => {
      // onclose 会跟随触发，由其负责重连。
    };
  }

  dispose(): void {
    this.disposed = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
  }

  send(content: string, media: string[] = []): string {
    const clientMessageId = crypto.randomUUID();
    this.pendingIds.add(clientMessageId);
    this.sendFrame({ type: "send", client_message_id: clientMessageId, content, media });
    return clientMessageId;
  }

  private setStatus(status: ConnectionStatus): void {
    if (this.status === status) return;
    this.status = status;
    this.events.onStatus(status);
  }

  private scheduleReconnect(): void {
    if (this.disposed) return;
    this.setStatus("offline");
    const delay = Math.min(
      RECONNECT_MAX_MS,
      RECONNECT_BASE_MS * 2 ** this.reconnectAttempt,
    );
    this.reconnectAttempt += 1;
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  private handleFrame(frame: ServerFrame): void {
    switch (frame.type) {
      case "hello": {
        if (frame.protocol_version > PROTOCOL_VERSION) {
          // 协议不兼容：不再重连，保持 offline。
          this.disposed = true;
          this.setStatus("offline");
          this.ws?.close();
          this.ws = null;
          return;
        }
        this.reconnectAttempt = 0;
        this.setStatus("online");
        // 重连时先尝试 WS 补拉，gap 超出服务端 buffer 时由 replay_required
        // 触发 REST 重建；首次连接（lastSeq=0）直接拉历史。
        if (this.lastSeq > 0) {
          this.sendFrame({ type: "replay", after_seq: this.lastSeq });
        } else {
          void this.loadHistory();
        }
        return;
      }
      case "message.accepted": {
        this.pendingIds.delete(frame.client_message_id);
        if (typeof frame.seq === "number" && frame.seq > this.lastSeq) {
          this.lastSeq = frame.seq;
        }
        break;
      }
      case "turn.completed":
      case "turn.failed": {
        if (typeof frame.seq === "number" && frame.seq > this.lastSeq) {
          this.lastSeq = frame.seq;
        }
        break;
      }
      case "replay_required": {
        // 服务端 buffer 不覆盖 after_seq：走 REST 重建。
        void this.loadHistory();
        return;
      }
      default:
        break;
    }
    this.events.onFrame(frame);
  }

  private sendFrame(frame: ClientFrame): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(frame));
    }
  }

  /** REST 全量重建（首次加载与 replay gap 时）。 */
  private async loadHistory(): Promise<void> {
    try {
      const resp = await fetch(
        `/api/chat/sessions/${encodeURIComponent("chat:local")}/messages?page_size=${HISTORY_PAGE_SIZE}&sort_order=asc`,
      );
      if (!resp.ok) return;
      const body = (await resp.json()) as { items?: Array<Record<string, unknown>> };
      const items = body.items ?? [];
      const messages: HistoryMessage[] = items.map((item) => ({
        seq: Number(item.seq ?? 0),
        role: item.role === "assistant" ? "assistant" : "user",
        content: String(item.content ?? ""),
        thinking: (item.thinking as string | null) ?? null,
        createdAt: (item.created_at as string | null) ?? null,
      }));
      this.events.onHistory(messages);
    } catch {
      // 网络失败：保持当前状态，下次重连再试。
    }
  }
}
