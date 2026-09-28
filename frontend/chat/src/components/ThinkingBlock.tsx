import { useEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import { StatusLabel } from "./StatusLabel";

/**
 * 可折叠思考框。active（思考流式进行中且为末位 part）时自动展开并显示
 * shimmer 标题；结束后自动折叠为「已思考 X 秒」。用户手动展开/折叠后
 * pinned 接管，自动状态不再覆盖。高度过渡用 grid-template-rows 0fr↔1fr。
 */
export function ThinkingBlock({ text, active }: { text: string; active: boolean }) {
  const [pinned, setPinned] = useState<boolean | null>(null);
  const startRef = useRef<number | null>(null);
  const [seconds, setSeconds] = useState<number | null>(null);

  useEffect(() => {
    if (active) {
      if (startRef.current === null) startRef.current = Date.now();
      return;
    }
    // active → false：结算思考时长
    if (startRef.current !== null) {
      setSeconds(Math.max(1, Math.round((Date.now() - startRef.current) / 1000)));
      startRef.current = null;
    }
  }, [active]);

  const open = pinned ?? active;
  return (
    <div className="thinking-block">
      <button
        type="button"
        className="thinking-head"
        aria-expanded={open}
        onClick={() => setPinned(!(pinned ?? active))}
      >
        {active ? (
          <StatusLabel category="thinking" />
        ) : (
          <span>已思考{seconds !== null ? ` ${seconds} 秒` : ""}</span>
        )}
        <ChevronDown size={14} className={`thinking-chevron ${open ? "open" : ""}`} />
      </button>
      <div className={`thinking-body ${open ? "open" : ""}`}>
        <div>
          <div className="thinking-content">{text}</div>
        </div>
      </div>
    </div>
  );
}
