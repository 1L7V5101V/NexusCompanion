import { useMemo, type ReactNode } from "react";

/**
 * 流式 Markdown 渲染：零依赖轻量解析（标题/段落/无序有序列表/围栏代码块/
 * 引用 + 行内粗斜体/行内代码/链接）。块按出现顺序以索引为 key——流式追加时
 * 已渲染块的身份稳定、不重挂载不闪烁，新块以 150ms 淡入；末位块可带光标。
 */

type Block =
  | { kind: "p"; text: string }
  | { kind: "h"; level: 1 | 2 | 3; text: string }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] }
  | { kind: "code"; lang: string; text: string; closed: boolean }
  | { kind: "quote"; text: string };

function parseBlocks(src: string): Block[] {
  const lines = src.split("\n");
  const blocks: Block[] = [];
  let para: string[] = [];
  let list: { kind: "ul" | "ol"; items: string[] } | null = null;
  let quote: string[] = [];
  let code: { lang: string; lines: string[] } | null = null;

  const flushPara = () => {
    if (para.length) blocks.push({ kind: "p", text: para.join("\n") });
    para = [];
  };
  const flushList = () => {
    if (list) blocks.push(list);
    list = null;
  };
  const flushQuote = () => {
    if (quote.length) blocks.push({ kind: "quote", text: quote.join("\n") });
    quote = [];
  };
  const flushAll = () => {
    flushPara();
    flushList();
    flushQuote();
  };

  for (const line of lines) {
    if (code) {
      if (/^```/.test(line)) {
        blocks.push({ kind: "code", lang: code.lang, text: code.lines.join("\n"), closed: true });
        code = null;
      } else {
        code.lines.push(line);
      }
      continue;
    }
    const fence = line.match(/^```\s*(\S*)/);
    if (fence) {
      flushAll();
      code = { lang: fence[1] ?? "", lines: [] };
      continue;
    }
    if (!line.trim()) {
      flushAll();
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.*)/);
    if (heading) {
      flushAll();
      blocks.push({ kind: "h", level: heading[1].length as 1 | 2 | 3, text: heading[2] });
      continue;
    }
    const ul = line.match(/^\s*[-*•]\s+(.*)/);
    if (ul) {
      flushPara();
      flushQuote();
      if (!list || list.kind !== "ul") {
        flushList();
        list = { kind: "ul", items: [] };
      }
      list.items.push(ul[1]);
      continue;
    }
    const ol = line.match(/^\s*\d+[.、]\s+(.*)/);
    if (ol) {
      flushPara();
      flushQuote();
      if (!list || list.kind !== "ol") {
        flushList();
        list = { kind: "ol", items: [] };
      }
      list.items.push(ol[1]);
      continue;
    }
    const quoteLine = line.match(/^>\s?(.*)/);
    if (quoteLine) {
      flushPara();
      flushList();
      quote.push(quoteLine[1]);
      continue;
    }
    flushList();
    flushQuote();
    para.push(line);
  }
  if (code) blocks.push({ kind: "code", lang: code.lang, text: code.lines.join("\n"), closed: false });
  flushAll();
  return blocks;
}

function renderInline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const re =
    /(\*\*([^*]+)\*\*)|(\*([^*\n]+)\*)|(`([^`]+)`)|(\[([^\]]+)\]\(([^)\s]+)\))/g;
  let last = 0;
  let key = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    if (m[1]) nodes.push(<strong key={key++}>{m[2]}</strong>);
    else if (m[3]) nodes.push(<em key={key++}>{m[4]}</em>);
    else if (m[5]) nodes.push(<code key={key++} className="md-code-inline">{m[6]}</code>);
    else if (m[7])
      nodes.push(
        <a key={key++} href={m[9]} target="_blank" rel="noreferrer" className="md-link">
          {m[8]}
        </a>,
      );
    last = m.index + m[0].length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function Cursor() {
  return (
    <span className="stream-cursor" aria-hidden="true">
      ▍
    </span>
  );
}

function BlockView({ block, cursor }: { block: Block; cursor: boolean }) {
  switch (block.kind) {
    case "p":
      return <p className="md-p">{renderInline(block.text)}{cursor ? <Cursor /> : null}</p>;
    case "h": {
      const Tag = (`h${block.level}` as const) satisfies "h1" | "h2" | "h3";
      return (
        <Tag className={`md-h${block.level}`}>
          {renderInline(block.text)}
          {cursor ? <Cursor /> : null}
        </Tag>
      );
    }
    case "ul":
      return (
        <ul className="md-list">
          {block.items.map((item, i) => (
            <li key={i}>{renderInline(item)}</li>
          ))}
          {cursor ? <Cursor /> : null}
        </ul>
      );
    case "ol":
      return (
        <ol className="md-list">
          {block.items.map((item, i) => (
            <li key={i}>{renderInline(item)}</li>
          ))}
          {cursor ? <Cursor /> : null}
        </ol>
      );
    case "code":
      return (
        <pre className="md-pre">
          <code>{block.text}</code>
          {cursor ? <Cursor /> : null}
        </pre>
      );
    case "quote":
      return (
        <blockquote className="md-quote">
          {renderInline(block.text)}
          {cursor ? <Cursor /> : null}
        </blockquote>
      );
  }
}

export function StreamingMarkdown({ text, streaming }: { text: string; streaming: boolean }) {
  const blocks = useMemo(() => parseBlocks(text), [text]);
  return (
    <div className="md">
      {blocks.map((block, i) => (
        <BlockView key={i} block={block} cursor={streaming && i === blocks.length - 1} />
      ))}
    </div>
  );
}
