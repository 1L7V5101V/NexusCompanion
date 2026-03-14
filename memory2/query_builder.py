from __future__ import annotations


def build_procedure_queries(user_msg: str, context_hint: str = "") -> list[str]:
    """为 procedure 检索生成多角度 query 列表。

    Phase 0 只做两件事：
    1. 保留原始消息，去掉有害的“操作规范”硬编码后缀
    2. 对少数高价值领域词做安全的 query 补强

    对未命中领域关键词的普通消息，保持保守策略，只返回原始消息。
    """
    msg = _normalize_text(user_msg)
    hint = _normalize_text(context_hint)
    if not msg:
        return [hint] if hint else []

    combined = f"{msg} {hint}".lower()
    queries = [msg]

    # 1. 先根据显式领域词补一条更贴近已存 summary 的动作 query。
    if _contains_any(combined, ("bilibili", "bilibili.com", "b23.tv", "b站")):
        if _contains_any(combined, ("rss", "订阅")):
            queries.append("bilibili RSS 订阅")
        if _contains_any(combined, ("下载", "视频", "链接", "发给我", "保存")):
            queries.append("B站 视频 下载 链接")

    # 2. 再按常见动作意图补充更短的检索词，避免只复读原文。
    if _contains_any(combined, ("rss", "订阅")):
        queries.append("RSS 订阅")
    if _contains_any(combined, ("搜", "搜索", "查", "检索", "grep", "关键字", "代码")):
        queries.append("代码 关键字 搜索")
    if _contains_any(combined, ("下载", "发给我", "保存", "导出")):
        queries.append("视频 下载" if "视频" in combined else "下载")

    # 3. 最后去重并过滤空串，保持输出稳定。
    return _unique_queries(queries)


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").split())


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _unique_queries(queries: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for query in queries:
        normalized = _normalize_text(query)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result
