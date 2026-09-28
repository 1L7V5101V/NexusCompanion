import { useEffect, useState } from "react";
import { useRotatingStatus } from "../hooks/useRotatingStatus";
import { STATUS_WORDS, UI_LANG, type StatusCategory, type StatusLang } from "../statusWords";

/**
 * 状态词标签：随机轮换词 + shimmer 扫光 + 词尾三点依次闪烁。
 * 固定高度 overflow:hidden 容器 + 按词库最长词估算 min-width——
 * 切换时旧词上移淡出（150ms）、新词自下 6px 淡入上移（200ms），
 * transform/opacity 实现，不引起相邻元素抖动。
 * specific 提供时显示固定具体文案（如工具名映射），不轮换。
 */
export function StatusLabel({
  category,
  specific,
  lang = UI_LANG,
}: {
  category: StatusCategory;
  specific?: string;
  lang?: StatusLang;
}) {
  const word = useRotatingStatus(category, { specific, lang });
  const [words, setWords] = useState({ cur: word, prev: null as string | null });

  useEffect(() => {
    setWords((w) => (word === w.cur ? w : { cur: word, prev: w.cur }));
    if (word === words.cur) return;
    const timer = setTimeout(
      () => setWords((w) => ({ cur: w.cur, prev: null })),
      220,
    );
    return () => clearTimeout(timer);
  }, [word]);

  const bank = STATUS_WORDS[lang][category];
  const maxLen = bank.reduce((m, w) => Math.max(m, w.length), 0);
  // 中文全宽字符约 1em/字，英文约 0.62em/字；+2em 给词尾省略号与缓冲
  const minWidth =
    lang === "zh"
      ? `${maxLen + 2}em`
      : `${Math.ceil(maxLen * 0.62) + 2}em`;

  const renderWord = (w: string, cls: string) => {
    const clean = w.replace(/…$|\.{3}$/, "");
    return (
      <span key={cls + w} className={`status-word ${cls}`}>
        <span className="thinking-shimmer">
          {clean}
          <span className="status-dots" aria-hidden="true">
            <span>.</span>
            <span>.</span>
            <span>.</span>
          </span>
        </span>
      </span>
    );
  };

  return (
    <span className="status-label" style={{ minWidth }}>
      {words.prev ? renderWord(words.prev, "status-word-leave") : null}
      {renderWord(words.cur, words.prev ? "status-word-enter" : "")}
    </span>
  );
}
