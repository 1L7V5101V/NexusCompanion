import { useEffect, useState } from "react";
import { StatusLabel } from "./StatusLabel";

/**
 * 等待/思考中的三点跳动指示器。active=false 时先 150ms 淡出再卸载，
 * 避免首个内容到达时圆点突然消失。withStatus：旁挂随机轮换状态词。
 */
export function ThinkingDots({
  active,
  withStatus = false,
}: {
  active: boolean;
  withStatus?: boolean;
}) {
  const [mounted, setMounted] = useState(active);

  useEffect(() => {
    if (active) {
      setMounted(true);
      return;
    }
    const timer = setTimeout(() => setMounted(false), 180);
    return () => clearTimeout(timer);
  }, [active]);

  if (!mounted) return null;
  return (
    <div
      className={`thinking-dots ${active ? "" : "thinking-dots-hide"}`}
      role="status"
      aria-label="正在思考"
    >
      <span className="thinking-dot" />
      <span className="thinking-dot" />
      <span className="thinking-dot" />
      {withStatus ? <StatusLabel category="thinking" /> : null}
    </div>
  );
}
