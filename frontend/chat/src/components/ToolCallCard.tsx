import { useState } from "react";
import { Check, ChevronDown, X } from "lucide-react";

export type ToolState = "running" | "complete" | "error";

/**
 * 工具调用卡片：默认只显示「状态图标 + 工具名 + 摘要」，点击展开完整
 * 返回内容（限高内部滚动）。running 旋转 spinner，success 对勾弹出，
 * error 红叉；多个工具各自独立更新。
 */
export function ToolCallCard({
  name,
  state,
  result,
}: {
  name: string;
  state: ToolState;
  result?: string;
}) {
  const [open, setOpen] = useState(false);
  const hasDetail = state !== "running" && !!result;
  return (
    <div className="tool-card">
      <button
        type="button"
        className="tool-head"
        aria-expanded={open}
        disabled={!hasDetail}
        onClick={() => setOpen((o) => !o)}
      >
        {state === "running" ? (
          <span className="tool-spinner" aria-hidden="true" />
        ) : state === "complete" ? (
          <Check size={14} className="tool-check" aria-hidden="true" />
        ) : (
          <X size={14} className="tool-x" aria-hidden="true" />
        )}
        <span className="tool-name">{name}</span>
        <span className="tool-summary">
          {state === "running" ? "执行中…" : state === "error" ? "失败" : "完成"}
        </span>
        {hasDetail ? (
          <ChevronDown size={14} className={`thinking-chevron ${open ? "open" : ""}`} />
        ) : null}
      </button>
      <div className={`thinking-body ${open ? "open" : ""}`}>
        <div>
          <pre className="tool-detail">{result}</pre>
        </div>
      </div>
    </div>
  );
}
