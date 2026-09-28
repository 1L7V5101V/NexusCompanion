/**
 * 状态词库（Claude 风格随机状态文案）。纯装饰，不影响真实状态判断；
 * 中英两套，跟随界面语言（navigator.language，zh 前缀 → 中文）。
 */

export type StatusLang = "zh" | "en";
export type StatusCategory = "thinking" | "tool_running" | "writing";

export const UI_LANG: StatusLang = navigator.language
  ?.toLowerCase()
  .startsWith("zh")
  ? "zh"
  : "en";

export const STATUS_WORDS: Record<StatusLang, Record<StatusCategory, string[]>> = {
  en: {
    thinking: [
      "Pondering",
      "Cogitating",
      "Ruminating",
      "Musing",
      "Mulling",
      "Contemplating",
      "Deliberating",
      "Percolating",
      "Synthesizing",
      "Noodling",
      "Marinating",
      "Brewing",
      "Untangling",
      "Connecting dots",
      "Weighing options",
      "Crunching",
      "Simmering",
      "Divining",
      "Conjuring",
      "Puzzling",
    ],
    tool_running: [
      "Digging through sources",
      "Looking it up",
      "Typing away",
      "Giving it a spin",
      "Excavating",
      "Cross-checking",
    ],
    writing: ["Organizing thoughts", "Phrasing it", "Putting it down", "Polishing"],
  },
  zh: {
    thinking: [
      "琢磨中",
      "推敲中",
      "思忖中",
      "沉吟中",
      "斟酌中",
      "构思中",
      "盘算中",
      "梳理思路",
      "捋一捋",
      "酝酿中",
      "发酵中",
      "灵光乍现中",
      "脑补中",
      "翻阅记忆",
      "拼凑线索",
      "权衡利弊",
    ],
    tool_running: ["翻找资料", "查阅中", "敲键盘中", "跑一下试试", "挖掘中", "核对信息"],
    writing: ["组织语言", "整理措辞", "落笔中", "润色中"],
  },
};

/** 已知工具名 → 具体文案（优先于随机词；正则对工具名做包含匹配）。 */
const TOOL_HINTS: { match: RegExp; zh: string; en: string }[] = [
  { match: /search|web_search|检索|搜索/i, zh: "正在搜索网页", en: "Searching the web" },
  { match: /read|file|读取|文件/i, zh: "正在读取文件", en: "Reading files" },
  { match: /code|run|exec|脚本|代码/i, zh: "正在运行代码", en: "Running code" },
  { match: /write|edit|写入|编辑/i, zh: "正在写入", en: "Writing changes" },
];

/** 按工具名取具体状态文案；无匹配返回 undefined（调用方回退随机词）。 */
export function toolHint(name: string, lang: StatusLang): string | undefined {
  return TOOL_HINTS.find((h) => h.match.test(name))?.[lang];
}
