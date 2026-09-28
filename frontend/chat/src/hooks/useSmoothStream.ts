import { useEffect, useRef, useState } from "react";

/**
 * 流式匀速消费 hook：业务层把完整目标文本写进来，渲染层用常驻 rAF 循环
 * 按固定节奏（每帧 1–3 字符，积压 >60 字符时按积压量加速）逐步放出，
 * 让输出速度平滑、避免 token 抖动。目标回缩（终态覆盖）时直接对齐。
 *
 * 循环以 ref 读取最新 target、不随 target 变化重启——delta 到达快过 rAF
 * 时，「每次 target 变化重启 effect 并 cancel 旧帧」会让 rAF 永远执行不到
 * （shown 恒为空）。追平时循环空转（无 setState），卸载时取消。
 * prefers-reduced-motion：跳过动画，直接显示目标文本。
 */
export function useSmoothStream(target: string): string {
  const prefersReduced =
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const [shown, setShown] = useState(() => (prefersReduced ? target : ""));
  const shownRef = useRef(prefersReduced ? target : "");
  const targetRef = useRef(target);
  targetRef.current = target;

  useEffect(() => {
    if (prefersReduced) {
      shownRef.current = targetRef.current;
      setShown(targetRef.current);
      return;
    }
    let cancelled = false;
    let raf = 0;
    const tick = () => {
      if (cancelled) return;
      const tgt = targetRef.current;
      // 目标回缩：终态覆盖流式内容时直接对齐
      if (tgt.length < shownRef.current.length) shownRef.current = tgt;
      const pending = tgt.length - shownRef.current.length;
      if (pending > 0) {
        // 每帧 1–3 字符；积压多时自动加速（上限 24/帧，防止无限落后）
        const step =
          pending > 60
            ? Math.min(24, Math.ceil(pending / 24))
            : Math.min(3, Math.max(1, Math.ceil(pending / 8)));
        shownRef.current = tgt.slice(0, shownRef.current.length + step);
        setShown(shownRef.current);
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => {
      cancelled = true;
      cancelAnimationFrame(raf);
    };
  }, [prefersReduced]);

  return shown;
}
