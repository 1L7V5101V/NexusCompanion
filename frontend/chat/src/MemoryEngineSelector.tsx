import { useEffect, useState } from "react";
import {
  fetchMemoryEngines,
  setActiveEngine,
  MemoryEnginesUnavailableError,
  type MemoryEnginesStatus,
} from "./memory";

/**
 * 聊天头部轻量 memory engine 选择器（C14）。
 *
 * - 仅 PG durable + auth 模式出现（端点 404 = dev/未装配 → 整体隐藏）；
 * - 展示服务端目录（能力/ready 状态/当前 active），不可用项禁选；
 * - 提交走 PUT /api/memory/engines/active（切换对下一个 work 生效）；
 * - 失败回滚选择并短暂显示服务端错误码。
 */
export function MemoryEngineSelector() {
  const [status, setStatus] = useState<MemoryEnginesStatus | null>(null);
  const [hidden, setHidden] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const next = await fetchMemoryEngines();
        if (!cancelled) setStatus(next);
      } catch (err) {
        if (!cancelled && err instanceof MemoryEnginesUnavailableError) {
          setHidden(true);
        }
        // 其他错误：保持隐藏前的静默（端点偶发失败不阻塞聊天）
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (hidden || !status) return null;

  const selectableCount = status.engines.filter((e) => e.selectable).length;
  if (selectableCount === 0) return null;

  const handleChange = async (engineId: string) => {
    if (pending || engineId === status.active_engine) return;
    setPending(true);
    setError("");
    try {
      const next = await setActiveEngine(engineId);
      setStatus((prev) =>
        prev
          ? {
              ...prev,
              active_engine: next.active_engine,
              tenant_policy_revision: next.tenant_policy_revision,
              engines: prev.engines.map((e) => ({
                ...e,
                active: e.engine_id === next.active_engine,
              })),
            }
          : prev,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "切换失败");
      setTimeout(() => setError(""), 4000);
    } finally {
      setPending(false);
    }
  };

  return (
    <label className="flex items-center gap-2 px-1">
      <span className="text-xs text-subtle">记忆引擎</span>
      <select
        aria-label="记忆引擎选择"
        value={status.active_engine}
        disabled={pending}
        onChange={(e) => void handleChange(e.target.value)}
        className="max-w-40 rounded-xl border border-white/10 bg-white/5 px-2 py-1 text-xs text-fg outline-none transition-colors hover:border-white/20 focus:border-white/30 disabled:opacity-50"
        title={error || "切换只对下一个对话生效"}
      >
        {status.engines.map((engine) => (
          <option
            key={engine.engine_id}
            value={engine.engine_id}
            disabled={!engine.selectable}
            className="bg-[#141414] text-fg"
          >
            {engine.display_name}
            {engine.active ? "（当前）" : ""}
            {!engine.ready ? " · 未就绪" : !engine.selectable ? " · 不可选" : ""}
          </option>
        ))}
      </select>
      {error ? (
        <span className="text-xs text-red-400" role="alert">
          {error}
        </span>
      ) : null}
    </label>
  );
}
