/**
 * 全屏背景：黑洞脉冲核心（WebGL，PulseCore abstract 变体）。
 *
 * - fixed 层 z -1：位于页面内容之下、body 底色之上（ui 表面需半透明才能透出）；
 * - PulseCore 按需懒加载（three + 双管线体积可观，不占首包）；
 * - WebGL 不可用时组件自带静态深空回退，不影响页面功能。
 */
import { lazy, Suspense } from "react";
import type { PulseCoreState } from "./pulsecore/PulseCorePipeline";

const PulseCore = lazy(() =>
  import("./pulsecore/PulseCore").then((m) => ({ default: m.PulseCore })),
);

export function PulseBackground({ state = "idle" }: { state?: PulseCoreState }) {
  return (
    <div className="pulse-bg" aria-hidden="true">
      <Suspense fallback={null}>
        <PulseCore
          variant="abstract"
          state={state}
          onInstance={(p) => {
            (window as unknown as Record<string, unknown>).__pulseBg = p;
          }}
        />
      </Suspense>
    </div>
  );
}
