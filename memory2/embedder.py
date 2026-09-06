"""
Embedding 客户端，对接 DashScope text-embedding-v3（OpenAI 兼容接口）
"""

from __future__ import annotations

import asyncio
import logging

from agent.admission.resources import ResourceKind
from agent.admission.resources import gate as admission_gate
from core.net.http import HttpRequester, RequestBudget, get_default_http_requester

logger = logging.getLogger(__name__)


class Embedder:
    MAX_BATCH = 10  # DashScope 每批上限
    MAX_TEXT_LEN = 2000

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "text-embedding-v3",
        output_dimensionality: int | None = None,
        requester: HttpRequester | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/embeddings"
        self._key = api_key
        self._model = model
        self._output_dimensionality = output_dimensionality
        self._requester = requester or get_default_http_requester("external_default")

    async def embed(self, text: str) -> list[float]:
        """单条 embed"""
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """分批 embed，每批 ≤ MAX_BATCH，批间 sleep 0.3s。

        C3 §5.9.5：全程持有 embedding 资源许可（embed() 委托本方法，单次取许可）。
        """
        async with admission_gate(ResourceKind.EMBEDDING):
            return await self._embed_batch_admitted(texts)

    async def _embed_batch_admitted(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        truncated = [t[: self.MAX_TEXT_LEN] for t in texts]

        for i in range(0, len(truncated), self.MAX_BATCH):
            batch = truncated[i : i + self.MAX_BATCH]
            payload: dict[str, object] = {"model": self._model, "input": batch}
            if self._output_dimensionality is not None:
                payload["dimensions"] = self._output_dimensionality
            resp = await self._requester.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout_s=30.0,
                budget=RequestBudget(total_timeout_s=40.0),
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            data.sort(key=lambda x: x["index"])
            results.extend(d["embedding"] for d in data)

            if i + self.MAX_BATCH < len(truncated):
                await asyncio.sleep(0.3)

        return results

    async def aclose(self) -> None:
        return None
