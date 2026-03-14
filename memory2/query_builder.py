from __future__ import annotations


def build_procedure_queries(user_msg: str, context_hint: str = "") -> list[str]:
    """为 procedure 检索生成 query 列表。

    Refactor 后只保留保守策略：直接使用原始消息，不再做领域关键词扩展。
    """
    msg = _normalize_text(user_msg)
    hint = _normalize_text(context_hint)
    if not msg:
        return [hint] if hint else []
    return [msg]


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").split())
