from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path

import openai

from agent.config import load_config
from memory2.retriever import Retriever
from memory2.store import MemoryStore2

DEFAULT_BENCHMARK_DB = Path("/tmp/akasic_benchmark/memory2.db")
SCOPE_CHANNEL = "locomo_benchmark"


class _SyncEmbedder:
    MAX_TEXT_LEN = 2000

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self._client = openai.OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._lock = threading.Lock()

    async def embed(self, text: str) -> list[float]:
        return await asyncio.to_thread(self._embed_sync, text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._embed_batch_sync, texts)

    def embed_sync(self, text: str) -> list[float]:
        return self._embed_sync(text)

    def _embed_sync(self, text: str) -> list[float]:
        text = (text or "").strip()[: self.MAX_TEXT_LEN]
        if not text:
            return []
        with self._lock:
            response = self._client.embeddings.create(model=self._model, input=[text])
        return response.data[0].embedding

    BATCH_SIZE = 10  # DashScope text-embedding-v3 单次最多 10 条

    def _embed_batch_sync(self, texts: list[str]) -> list[list[float]]:
        """批量 embed：一次 API 请求处理多条文本，比逐条调用快 ~20x。"""
        clean = [(t or "").strip()[: self.MAX_TEXT_LEN] for t in texts]
        result: list[list[float]] = [[] for _ in clean]
        non_empty = [(i, t) for i, t in enumerate(clean) if t]
        if not non_empty:
            return result
        for chunk_start in range(0, len(non_empty), self.BATCH_SIZE):
            chunk = non_empty[chunk_start: chunk_start + self.BATCH_SIZE]
            indices, batch_texts = zip(*chunk)
            with self._lock:
                resp = self._client.embeddings.create(
                    model=self._model, input=list(batch_texts)
                )
            embeddings = [item.embedding for item in sorted(resp.data, key=lambda x: x.index)]
            for idx, emb in zip(indices, embeddings):
                result[idx] = emb
        return result


@dataclass
class BenchmarkComponents:
    store: MemoryStore2
    embedder: _SyncEmbedder
    retriever: Retriever
    llm_client: openai.OpenAI        # 主模型，用于 response/evaluate
    light_llm_client: openai.OpenAI  # light 模型，用于 ingest 抽取
    model: str
    light_model: str


def load_benchmark_components(
    config_path: str | Path = "config.json",
    db_path: str | Path | None = None,
    light_model_override: str | None = None,
    score_threshold_event_override: float | None = None,
    inject_max_event_profile_override: int | None = None,
) -> BenchmarkComponents:
    # 1. 读取项目配置，保留现有 ${ENV_VAR} 解析逻辑。
    config = load_config(config_path)

    # 2. benchmark 必须使用独立 DB，避免污染生产记忆库。
    resolved_db = Path(db_path).expanduser() if db_path else DEFAULT_BENCHMARK_DB

    # 3. embedder 优先走 light 端点，模型固定用 memory_v2.embed_model。
    embed_base_url = config.light_base_url or config.base_url
    embed_api_key = config.light_api_key or config.api_key
    embedder = _SyncEmbedder(
        api_key=embed_api_key,
        base_url=embed_base_url or "",
        model=config.memory_v2.embed_model,
    )

    # 4. Retriever 参数全部来自 config.memory_v2。
    store = MemoryStore2(resolved_db)
    retriever = Retriever(
        store=store,
        embedder=embedder,
        top_k=config.memory_v2.top_k_history,
        score_threshold=config.memory_v2.score_threshold,
        score_thresholds={
            "event": score_threshold_event_override if score_threshold_event_override is not None else config.memory_v2.score_threshold_event,
            "profile": config.memory_v2.score_threshold_profile,
            "procedure": config.memory_v2.score_threshold_procedure,
            "preference": config.memory_v2.score_threshold_preference,
        },
        relative_delta=config.memory_v2.relative_delta,
        inject_max_chars=config.memory_v2.inject_max_chars,
        inject_max_forced=config.memory_v2.inject_max_forced,
        inject_max_procedure_preference=config.memory_v2.inject_max_procedure_preference,
        inject_max_event_profile=inject_max_event_profile_override if inject_max_event_profile_override is not None else config.memory_v2.inject_max_event_profile,
        inject_line_max=config.memory_v2.inject_line_max,
        procedure_guard_enabled=config.memory_v2.procedure_guard_enabled,
    )

    # 5. 主 LLM 客户端（response/evaluate）走主模型配置。
    llm_client = openai.OpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
    )
    model = config.model
    light_model = light_model_override or config.light_model or config.model

    # 6. light LLM 客户端（ingest 抽取）走 light 端点，避免 URL/key 不匹配。
    light_llm_client = openai.OpenAI(
        api_key=config.light_api_key or config.api_key,
        base_url=config.light_base_url or config.base_url,
    )

    return BenchmarkComponents(
        store=store,
        embedder=embedder,
        retriever=retriever,
        llm_client=llm_client,
        light_llm_client=light_llm_client,
        model=model,
        light_model=light_model,
    )


__all__ = [
    "BenchmarkComponents",
    "DEFAULT_BENCHMARK_DB",
    "SCOPE_CHANNEL",
    "_SyncEmbedder",
    "load_benchmark_components",
]
