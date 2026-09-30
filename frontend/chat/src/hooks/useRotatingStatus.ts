import { useEffect, useRef, useState } from "react";
import { STATUS_WORDS, UI_LANG, type StatusCategory, type StatusLang } from "../statusWords";

/**
 * 随机状态词轮换：挂载时随机取一个，之后每 2.5–4s（区间内随机）换下一个。
 * 洗牌袋语义——一轮词库用完前不重复（用完重洗），且不允许连续两次相同。
 * specific 提供时（如已知工具名的具体文案）直接使用、不轮换。
 * prefers-reduced-motion：轮换间隔拉长到 5s（动画降级在 CSS）。
 */

const bags = new Map<string, string[]>();
const lasts = new Map<string, string>();

function pickFromBag(category: StatusCategory, lang: StatusLang, last: string | null): string {
  const key = `${category}:${lang}`;
  const bank = STATUS_WORDS[lang][category];
  let bag = bags.get(key);
  if (!bag || bag.length === 0) {
    bag = [...bank];
    for (let i = bag.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [bag[i], bag[j]] = [bag[j], bag[i]];
    }
    // 重洗后首个与上一个不同，避免跨轮连续重复
    const lastWord = lasts.get(key) ?? last;
    if (bag.length > 1 && bag[0] === lastWord) {
      [bag[0], bag[1]] = [bag[1], bag[0]];
    }
  }
  const word = bag.shift() as string;
  bags.set(key, bag);
  lasts.set(key, word);
  return word;
}

export function useRotatingStatus(
  category: StatusCategory,
  opts?: { specific?: string; lang?: StatusLang; active?: boolean },
): string {
  const { specific, lang = UI_LANG, active = true } = opts ?? {};
  const prefersReduced =
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const [word, setWord] = useState(() => specific ?? pickFromBag(category, lang, null));
  const lastRef = useRef(word);

  useEffect(() => {
    if (specific) {
      setWord(specific);
      lastRef.current = specific;
      return;
    }
    if (!active) return;
    const next = pickFromBag(category, lang, lastRef.current);
    lastRef.current = next;
    setWord(next);
    let timer: number;
    const schedule = () => {
      const delay = prefersReduced ? 5000 : 2500 + Math.random() * 1500;
      timer = window.setTimeout(() => {
        const word2 = pickFromBag(category, lang, lastRef.current);
        lastRef.current = word2;
        setWord(word2);
        schedule();
      }, delay);
    };
    schedule();
    return () => clearTimeout(timer);
  }, [category, specific, lang, active, prefersReduced]);

  return word;
}
