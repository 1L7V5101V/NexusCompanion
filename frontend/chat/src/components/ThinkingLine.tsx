import { StatusLabel } from "./StatusLabel";
import { toolHint, UI_LANG, type StatusCategory } from "../statusWords";

/**
 * 思考中状态行：一行左对齐的流光文字，直接浮在页面底色上——
 * 无气泡、无背景、无边框。工具运行时复用同一行，仅把词库切到
 * tool_running（已知工具名时优先具体文案）；正文起步过渡时切到
 * writing。hide 时淡出让位给正文。
 */
export function ThinkingLine({
  category,
  toolName,
  hide = false,
}: {
  category: StatusCategory;
  toolName?: string;
  hide?: boolean;
}) {
  const specific = category === "tool_running" && toolName ? toolHint(toolName, UI_LANG) : undefined;
  return (
    <div className={`thinking-line ${hide ? "thinking-line-hide" : ""}`} role="status">
      <StatusLabel category={category} specific={specific} />
    </div>
  );
}
