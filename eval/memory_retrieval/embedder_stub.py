"""确定性 stub embedder（ADR-7）：jieba 分词 + sha1 桶映射，256 维 L2 归一化。

对外是 `async def embed(text) -> list[float]`，与 `memory2.embedder.Embedder`
同接口；不调外部 API，评测零成本、零网络。词面重合 → 向量余弦高，因此本
评测衡量的是管线（归一化/融合/RRF/开关）而非 embedding 模型质量（§10
DEFERRED BY EVIDENCE）。

附带 `FailingEmbedder`：embed 抛异常，用于记录注入 embed 异常后的 keyword-only
退化幅度（metrics.degrade_reports）。
"""

from __future__ import annotations

import hashlib

from memory2.tokenizer import tokenize_query

_EMBED_DIM = 256


class EmbedderStub:
    """hashed bag-of-words：每个 token 摇到固定符号桶，词袋相加后 L2 归一化。"""

    def __init__(self, dim: int = _EMBED_DIM) -> None:
        self._dim = dim

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for term in tokenize_query(text or ""):
            digest = hashlib.sha1(term.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "little") % self._dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[bucket] += sign
        return _l2_normalize(vec)


class FailingEmbedder:
    """仿真向量 lane 失败：embed 恒抛异常，检索应 fail-open 走 keyword lane。"""

    async def embed(self, text: str) -> list[float]:
        del text
        raise RuntimeError("embed history embedding service unavailable")


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = sum(v * v for v in vec) ** 0.5
    if norm < 1e-12:
        return vec
    return [v / norm for v in vec]