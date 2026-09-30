import { useState } from "react";
import type { ChatMessage } from "../store";

type ProcessPart = Exclude<ChatMessage["parts"][number], { kind: "text" }>;

/**
 * 过程入口：思考/工具过程折叠为一行次要色小字（如「已思考 · 调用了工具」），
 * 点击展开缩进详情——详情同样无背景无边框，用缩进 + 细线区分层级。
 */
export function ProcessEntry({ parts }: { parts: ProcessPart[] }) {
  const [open, setOpen] = useState(false);
  const thought = parts.some((p) => p.kind === "reasoning");
  const toolCount = parts.filter((p) => p.kind === "tool").length;
  const label = [thought ? "已思考" : null, toolCount ? "调用了工具" : null]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="process-entry">
      <button
        type="button"
        className="process-toggle"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {label}
      </button>
      <div className={`process-body ${open ? "open" : ""}`}>
        <div>
          <div className="process-detail">
            {parts.map((part, i) =>
              part.kind === "reasoning" ? (
                <div key={i} className="process-think">
                  {part.text}
                </div>
              ) : (
                <div key={i}>
                  <span className="process-tool-name">{part.toolName}</span>
                  <span className="process-tool-status">
                    {part.state === "error" ? "失败" : "完成"}
                  </span>
                  {part.result ? (
                    <div className="process-tool-result">{part.result}</div>
                  ) : null}
                </div>
              ),
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
