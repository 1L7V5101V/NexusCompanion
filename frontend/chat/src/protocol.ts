/**
 * WebChat dev v0 帧协议类型。与 tests/fixtures/chat_protocol_frames.json
 * 和 infra/channels/web_chat_protocol.py 保持一致；改动必须三处同步。
 */

export const PROTOCOL_VERSION = 0;
export const DEV_SESSION_KEY = "chat:local";
/** dev-only 单用户身份（P0.5）：无 C5 认证前的显式回退，P1 由 C5/C1 注入。 */
export const DEV_ACCOUNT_ID = "dev:local";
export const DEV_TENANT_ID = "default";
export const CLOSE_OVERLOAD = 1013;
/** 连接空闲超时：服务端回收静默连接（§5.9.4 连接生命周期）。 */
export const CLOSE_IDLE_TIMEOUT = 1001;
/** dev-only 门禁拒绝：非 dev 模式或非回环客户端。 */
export const CLOSE_DEV_ONLY = 1008;
/** 客户端 keepalive 周期（服务端空闲超时 90s，留足余量）。 */
export const KEEPALIVE_INTERVAL_MS = 25_000;

/** error 帧 code 词表（与 tests/fixtures/chat_protocol_frames.json 对齐）。 */
export const ERROR_CODES = [
  "bad_frame",
  "unknown_type",
  "bad_client_message_id",
  "bad_request",
  "bad_replay",
  "overload",
  "dev_only",
] as const;

export type ErrorCode = (typeof ERROR_CODES)[number];

// Server → client
export type HelloFrame = {
  type: "hello";
  seq: number | null;
  connection_id: string;
  protocol_version: number;
  /** 服务端派生的身份三元组，仅用于展示/调试，不构成授权来源。 */
  account_id: string;
  tenant_id: string;
  conversation_id: string;
  session_key: string;
  latest_seq: number;
};

export type MessageAcceptedFrame = {
  type: "message.accepted";
  seq: number | null;
  client_message_id: string;
  session_key: string;
};

export type MessageDeltaFrame = {
  type: "message.delta";
  seq: number | null;
  turn_id: string;
  content_delta: string;
  thinking_delta: string;
};

export type ToolStartedFrame = {
  type: "tool.started";
  seq: number | null;
  turn_id: string;
  call_id: string;
  tool_name: string;
};

export type ToolCompletedFrame = {
  type: "tool.completed";
  seq: number | null;
  turn_id: string;
  call_id: string;
  tool_name: string;
  status: string;
  result_preview: string;
};

export type TurnCompletedFrame = {
  type: "turn.completed";
  seq: number | null;
  turn_id: string;
  content: string;
  thinking: string | null;
  media: string[];
};

export type TurnFailedFrame = {
  type: "turn.failed";
  seq: number | null;
  turn_id: string;
  error: string;
};

export type ReplayRequiredFrame = {
  type: "replay_required";
  seq: number | null;
  after_seq: number;
};

export type ErrorFrame = {
  type: "error";
  seq: number | null;
  code: string;
  message: string;
};

export type PongFrame = {
  type: "pong";
  seq: number | null;
};

export type ServerFrame =
  | HelloFrame
  | MessageAcceptedFrame
  | MessageDeltaFrame
  | ToolStartedFrame
  | ToolCompletedFrame
  | TurnCompletedFrame
  | TurnFailedFrame
  | ReplayRequiredFrame
  | ErrorFrame
  | PongFrame;

// Client → server
export type SendFrame = {
  type: "send";
  client_message_id: string;
  content: string;
  media?: string[];
};

export type ReplayFrame = {
  type: "replay";
  after_seq: number;
};

export type PingFrame = {
  type: "ping";
};

export type ClientFrame = SendFrame | ReplayFrame | PingFrame;

export function parseServerFrame(raw: string): ServerFrame | null {
  try {
    const value = JSON.parse(raw) as { type?: unknown };
    if (typeof value?.type !== "string") return null;
    return value as ServerFrame;
  } catch {
    return null;
  }
}
