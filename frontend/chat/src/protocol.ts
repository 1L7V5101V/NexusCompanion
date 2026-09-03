/**
 * WebChat dev v0 帧协议类型。与 tests/fixtures/chat_protocol_frames.json
 * 和 infra/channels/web_chat_protocol.py 保持一致；改动必须三处同步。
 */

export const PROTOCOL_VERSION = 0;
export const DEV_SESSION_KEY = "chat:local";
export const CLOSE_OVERLOAD = 1013;

// Server → client
export type HelloFrame = {
  type: "hello";
  seq: number | null;
  connection_id: string;
  protocol_version: number;
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
