import { useEffect, useRef, useState } from "react";

/**
 * 流式匀速消费 hook：业务层把完整目标文本写进来，渲染层用 rAF 按
 * 固定节奏（每帧 1–3 字符，积压 >60 字符时按积压量加速）逐步放出，
 * 让输出速度平滑、避免 token 抖动。目标回缩（终态覆盖）时直接对齐。
 * prefers-reduced-motion：跳过动画，直接显示目标文本。
 */
export function useSmoothStream(target: string): string {
  const prefersReduced =
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const [shown, setShown] = useState(() => (prefersReduced ? target : ""));
  const shownRef = useRef(prefersReduced ? target : "");
  const rafRef = useRef(0);

  useEffect(() => {
    if (prefersReduced) {
      shownRef.current = target;
      setShown(target);
      return;
    }
    let cancelled = false;
    const tick = () => {
      if (cancelled) return;
      // 目标回缩：终态覆盖流式内容时直接对齐
      if (target.length < shownRef.current.length) {
        shownRef.current = target;
        setShown(target);
        return;
      }
      const pending = target.length - shownRef.current.length;
      if (pending <= 0) return; // 已追平：停帧，等目标变化重新起帧
      // 每帧 1–3 字符；积压多时自动加速（上限 24/帧，防止无限落后）
      const step =
        pending > 60
          ? Math.min(24, Math.ceil(pending / 24))
          : Math.min(3, Math.max(1, Math.ceil(pending / 8)));
      shownRef.current = target.slice(0, shownRef.current.length + step);
      setShown(shownRef.current);
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      cancelled = true;
      cancelAnimationFrame(rafRef.current);
    };
  }, [target, prefersReduced]);

  return shown;
}
