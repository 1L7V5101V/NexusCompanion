"""sparse lane 查询分词：jieba 搜索粒度优先，regex bigram 回退。

作为 keyword lane 的统一 term 来源（LIKE 保底与 BM25/pg_search 查询构造共用）。
jieba 不可用（未安装/导入失败）时回退到与既有 `_extract_terms` 等价的 regex 策略，
保证行为不劣于改造前。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import 分支由 import 失败路径覆盖
    import jieba

    jieba.initialize()
    _JIEBA_AVAILABLE = True
except Exception:  # pragma: no cover - 环境缺 jieba 时走回退
    jieba = None  # type: ignore[assignment]
    _JIEBA_AVAILABLE = False

_ASCII_TOKEN_RE = re.compile(r"[a-zA-Z0-9_\-\.]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]{2,}")

# 与旧 retriever._CJK_STOPWORDS 同表；通用高频词不进 term 集合。
CJK_STOPWORDS = {
    "用户", "助手", "我们", "他们", "这个", "那个", "什么", "如何", "是否",
    "有没", "没有", "有过", "做过", "进行", "完成", "包括", "通过", "实现",
    "行为", "内容", "相关", "情况", "问题", "方式", "时候", "时间", "目前",
    "当前", "最近", "之前", "以前", "后来", "然后", "因为", "所以", "但是",
    "用户在", "用户对", "的行为吗", "进行了",
}

MAX_TERMS = 20


def is_jieba_available() -> bool:
    return _JIEBA_AVAILABLE


def tokenize_query(query: str) -> list[str]:
    """提取检索 term：ASCII token + CJK 分词，去重保序，上限 MAX_TERMS。"""
    text = (query or "").strip()
    if not text:
        return []
    if _JIEBA_AVAILABLE:
        return _tokenize_with_jieba(text)
    return _tokenize_with_regex(text)


def _push_term(terms: list[str], seen: set[str], term: str) -> None:
    cleaned = term.strip()
    if not cleaned or cleaned in seen:
        return
    seen.add(cleaned)
    terms.append(cleaned)


def _tokenize_with_jieba(query: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in _ASCII_TOKEN_RE.findall(query):
        _push_term(terms, seen, token.lower())
    for match in _CJK_RUN_RE.finditer(query):
        run = match.group()
        # lcut_for_search 输出重叠粒度（长词 + 子词），对字面命中最友好。
        for word in jieba.lcut_for_search(run):  # type: ignore[union-attr]
            token = str(word).strip()
            if len(token) < 2 or token in CJK_STOPWORDS:
                continue
            _push_term(terms, seen, token)
    return terms[:MAX_TERMS]


def _tokenize_with_regex(query: str) -> list[str]:
    """regex 回退：与改造前 _extract_terms 等价（ASCII token + CJK bigram）。"""
    terms: list[str] = []
    seen: set[str] = set()
    for token in _ASCII_TOKEN_RE.findall(query):
        _push_term(terms, seen, token.lower())
    for match in _CJK_RUN_RE.finditer(query):
        chunk = match.group()
        if len(chunk) <= 4:
            if chunk not in CJK_STOPWORDS:
                _push_term(terms, seen, chunk)
            continue
        for i in range(len(chunk) - 1):
            bigram = chunk[i : i + 2]
            if bigram not in CJK_STOPWORDS:
                _push_term(terms, seen, bigram)
    return terms[:MAX_TERMS]
