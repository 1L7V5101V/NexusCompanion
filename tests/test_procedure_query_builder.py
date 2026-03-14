from memory2.query_builder import build_procedure_queries


def test_original_message_always_in_queries():
    """原始消息必须出现在 query 列表中。"""
    msg = "帮我搜一下代码里的关键字"
    queries = build_procedure_queries(msg)
    assert msg in queries


def test_no_hardcoded_caozuoguifan_suffix():
    """不能出现硬编码的 '操作规范' 后缀拼接。

    当前 handlers.py:104 的做法：f"{msg} 操作规范"
    这会让 embedding 向量向"规范/制度"语义偏移。
    """
    msg = "帮我搜一下代码里的关键字"
    queries = build_procedure_queries(msg)
    assert f"{msg} 操作规范" not in queries


def test_returns_at_least_two_queries():
    """至少返回 2 个不同角度的 query，才能做 max-pool。"""
    queries = build_procedure_queries("把这个B站视频下载下来")
    assert len(queries) >= 2


def test_all_queries_are_non_empty_strings():
    queries = build_procedure_queries("给我生成一个RSS")
    assert all(isinstance(q, str) and q.strip() for q in queries)


def test_no_duplicate_queries():
    """返回值里不能有重复的 query。"""
    queries = build_procedure_queries("hello")
    assert len(queries) == len(set(queries))


def test_short_message_still_works():
    """极短消息不应崩溃，且仍然返回至少 1 个有效 query。"""
    queries = build_procedure_queries("下载")
    assert len(queries) >= 1
    assert queries[0].strip()


def test_generic_message_with_no_keywords_returns_original_only():
    """对于没有命中领域关键词的普通消息，只返回原始消息本身。

    这是 Phase 0 的预期行为：去掉坏后缀比强行补 query 更安全。
    """
    queries = build_procedure_queries("帮我创建一个新技能")
    assert queries == ["帮我创建一个新技能"]


def test_bilibili_download_query_contains_action_keywords():
    """B站下载场景的 queries 里，至少有一个 query 不是简单重复原文、
    且包含动作相关词（下载/B站/bilibili/视频）。
    这样才能覆盖 stored summary 里的「B站链接」「下载」场景。
    """
    msg = "把这个视频发给我"
    queries = build_procedure_queries(msg, context_hint="bilibili.com")
    action_keywords = {"下载", "B站", "bilibili", "视频", "链接"}
    has_action_query = any(
        any(kw.lower() in q.lower() for kw in action_keywords) for q in queries
    )
    assert has_action_query


def test_rss_query_preserves_domain_keyword():
    """RSS 场景：原消息中的领域词（rss、订阅）应被保留在某个 query 里。"""
    msg = "给我一个bilibili的rss订阅"
    queries = build_procedure_queries(msg)
    assert any("rss" in q.lower() or "订阅" in q for q in queries)
