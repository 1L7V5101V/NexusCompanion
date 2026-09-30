import { useExternalStoreRuntime } from "@assistant-ui/react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ChatStore, toThreadMessageLike } from "./store";

/**
 * ?mock=1 本地演示：复用真实 ChatStore.handleFrame 通道，按
 * thinking → tool_call(success) → tool_call(error) → 再思考 → 正文流式 → done
 * 的时序泵脚本帧。不改任何网络与业务逻辑；每次发送都会重放一遍。
 */

const THINKING_1 =
  "用户发来一句话，我先拆解意图：需要一份带结构的说明。计划分三步——先检索资料核对事实，再用一段脚本验证格式，最后组织成带标题和列表的回答。";
const THINKING_2 =
  "第二个工具超时了，不影响结论：检索结果已经足够，直接整理输出，并把失败情况如实告诉用户。";

const ANSWER = `## 整理结果

这是一段 **mock 流式输出**，用来验证消息渲染与动画：思考、工具调用与正文按到达顺序交错渲染，不合并。

- 三点跳动在首个内容到达后平滑淡出
- 工具卡片独立更新状态，点击可展开返回内容
- 正文由 useSmoothStream 匀速放出，末尾带闪烁光标

行内元素也支持：\`inline code\`、**粗体**、*斜体* 与 [链接](https://example.com)。

\`\`\`ts
// 围栏代码块
export const smooth = (tokens: string[]) => tokens.join("");
\`\`\`

> 引用块同样按块淡入，不会对已渲染内容重复触发动画。

以上即完整演示序列的终态。`;

function playScript(store: ChatStore, timers: number[]): void {
  const turnId = `mock-${Date.now()}`;
  let t = 315;
  // at(gapBefore, fn)：先推进时间轴 gapBefore 再触发 fn——gap 就是上一个
  // 事件到本事件的真实间隔（工具运行时长 = started 到 completed 的间隔）。
  const at = (gapBefore: number, fn: () => void) => {
    t += gapBefore;
    timers.push(window.setTimeout(fn, t));
  };

  // 1) 思考流式
  for (let i = 0; i < THINKING_1.length; i += 12) {
    const chunk = THINKING_1.slice(i, i + 12);
    at(85, () =>
      store.handleFrame({ type: "message.delta", turn_id: turnId, thinking_delta: chunk, content_delta: "" }),
    );
  }
  // 2) 工具 1：成功（运行 1.5s）
  at(250, () => store.handleFrame({ type: "tool.started", turn_id: turnId, call_id: "call-1", tool_name: "web_search" }));
  at(1500, () =>
    store.handleFrame({
      type: "tool.completed",
      turn_id: turnId,
      call_id: "call-1",
      tool_name: "web_search",
      status: "success",
      result_preview:
        '检索到 3 条相关资料：\n1. "流式 UI 渲染最佳实践" —— 建议按块渲染、增量追加\n2. "Chat 交互动效指南" —— 光标与匀速输出\n3. "Reduced Motion 清单" —— 跳动/扫光需可降级',
    }),
  );
  // 3) 工具 2：失败（运行 1.3s）
  at(300, () => store.handleFrame({ type: "tool.started", turn_id: turnId, call_id: "call-2", tool_name: "run_code" }));
  at(1300, () =>
    store.handleFrame({
      type: "tool.completed",
      turn_id: turnId,
      call_id: "call-2",
      tool_name: "run_code",
      status: "error",
      result_preview: "TimeoutError: execution exceeded 1000ms",
    }),
  );
  // 4) 再思考
  for (let i = 0; i < THINKING_2.length; i += 12) {
    const chunk = THINKING_2.slice(i, i + 12);
    at(85, () =>
      store.handleFrame({ type: "message.delta", turn_id: turnId, thinking_delta: chunk, content_delta: "" }),
    );
  }
  // 5) 正文流式（每帧一小段，末尾与 turn.completed 的终态完全一致）
  //    先留 800ms 过渡：writing 状态词在正文起步前可见
  at(800, () => {});
  const chunks: string[] = [];
  for (let i = 0; i < ANSWER.length; i += 7) chunks.push(ANSWER.slice(i, i + 7));
  for (const chunk of chunks) {
    at(45, () =>
      store.handleFrame({ type: "message.delta", turn_id: turnId, thinking_delta: "", content_delta: chunk }),
    );
  }
  // 6) 终态（source of truth，与流式累积一致）
  at(250, () =>
    store.handleFrame({
      type: "turn.completed",
      turn_id: turnId,
      content: ANSWER,
      thinking: THINKING_1 + THINKING_2,
      media: [],
    }),
  );
}

export function useMockChatRuntime(): {
  runtime: ReturnType<typeof useExternalStoreRuntime>;
  status: "online";
  isRunning: boolean;
  messages: ReturnType<ChatStore["getSnapshot"]>["messages"];
  awaitingFirstToken: boolean;
} {
  const store = useMemo(() => new ChatStore(), []);
  const [snapshot, setSnapshot] = useState(() => store.getSnapshot());
  useEffect(() => store.subscribe(() => setSnapshot(store.getSnapshot())), [store]);

  const onNew = useCallback(
    async (message: { content: readonly { type: string; text?: string }[] }) => {
      const text = message.content
        .map((part) => (part.type === "text" ? (part.text ?? "") : ""))
        .join("");
      const timers: number[] = [];
      store.appendUserMessage(() => `mock-user-${Date.now()}`, text);
      playScript(store, timers);
    },
    [store],
  );

  const runtime = useExternalStoreRuntime({
    messages: snapshot.messages.map(toThreadMessageLike),
    isRunning: snapshot.isRunning,
    onNew,
    onCancel: async () => {},
    convertMessage: (message) => message,
  });

  const lastMessage = snapshot.messages[snapshot.messages.length - 1];
  const awaitingFirstToken =
    snapshot.isRunning && (!lastMessage || lastMessage.role === "user");

  return {
    runtime,
    status: "online",
    isRunning: snapshot.isRunning,
    messages: snapshot.messages,
    awaitingFirstToken,
  };
}
