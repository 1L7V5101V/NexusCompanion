from __future__ import annotations

import logging
from typing import Any, Literal

from benchmark.config_loader import BenchmarkComponents, SCOPE_CHANNEL
from benchmark.episode_extractor import EpisodeExtractor, ExtractedFact

logger = logging.getLogger(__name__)

class AkasicMemAgent:
    def __init__(
        self,
        components: BenchmarkComponents,
        ingest_mode: Literal["gold", "llm"] = "llm",
    ) -> None:
        self._store = components.store
        self._embedder = components.embedder
        self._extractor = EpisodeExtractor(
            llm_client=components.light_llm_client,
            model=components.light_model,
        )
        self._ingest_mode = ingest_mode

    @staticmethod
    def _guess_gold_memory_type(summary: str) -> str:
        lowered = summary.lower()
        profile_markers = (
            " is ",
            " has ",
            " feels ",
            " likes ",
            " loves ",
            " prefers ",
            " works as ",
            " wants ",
            " believes ",
        )
        if any(marker in f" {lowered} " for marker in profile_markers):
            return "profile"
        return "event"

    def _upsert_fact(
        self,
        *,
        character: str,
        summary: str,
        memory_type: str,
        session_date: str | None,
        source_ref: str,
        category: str,
    ) -> bool:
        if not summary.strip():
            return False
        try:
            embedding = self._embedder.embed_sync(summary)
            if not embedding:
                return False
            self._store.upsert_item(
                memory_type=memory_type,
                summary=summary,
                embedding=embedding,
                source_ref=source_ref,
                extra={
                    "scope_channel": SCOPE_CHANNEL,
                    "scope_chat_id": character,
                    "category": category,
                },
                happened_at=session_date,
            )
            return True
        except Exception as exc:
            logger.warning("upsert_fact 失败 character=%s: %s", character, exc)
            return False

    def _build_gold_facts(
        self,
        gold_observations: dict[str, list[list[str]]] | None,
        session_date: str,
    ) -> list[ExtractedFact]:
        facts: list[ExtractedFact] = []
        if not gold_observations:
            return facts
        for character, entries in gold_observations.items():
            for row in entries or []:
                if not row:
                    continue
                raw_summary = str(row[0]).strip()
                if character.lower() not in raw_summary.lower():
                    summary = f"{character} - {raw_summary}"
                else:
                    summary = raw_summary
                memory_type = self._guess_gold_memory_type(summary)
                category = "personal_fact" if memory_type == "profile" else "event"
                facts.append(
                    ExtractedFact(
                        character=character,
                        summary=summary,
                        memory_type=memory_type,
                        category=category,
                        happened_at=session_date,
                    )
                )
        return facts

    def update_character_memory(
        self,
        session_data: list[dict],
        session_date: str,
        characters: list[str],
        gold_observations: dict | None = None,
        use_image: bool = False,
    ) -> dict[str, Any]:
        try:
            # 1. 先决定本 session 用 gold 还是 llm 提取。
            effective_mode = self._ingest_mode
            if effective_mode == "gold" and not gold_observations:
                effective_mode = "llm"

            # 2. 再批量提取事实。
            if effective_mode == "gold":
                facts = self._build_gold_facts(gold_observations, session_date)
            else:
                facts = self._extractor.extract_from_session(
                    session_utterances=session_data,
                    session_date=session_date,
                    characters=characters,
                )

            # 3. 最后按角色写入向量库并返回统计。
            update_results: dict[str, Any] = {
                character: {
                    "success": True,
                    "events_updated": False,
                    "profile_updated": False,
                    "events_count": 0,
                    "profile_count": 0,
                }
                for character in characters
            }
            for index, fact in enumerate(facts):
                if fact.character not in update_results:
                    continue
                source_ref = (
                    f"locomo:{fact.character}:{session_date}:{effective_mode}:{index}"
                )
                saved = self._upsert_fact(
                    character=fact.character,
                    summary=fact.summary,
                    memory_type=fact.memory_type,
                    session_date=fact.happened_at,
                    source_ref=source_ref,
                    category=fact.category,
                )
                if not saved:
                    continue
                if fact.memory_type == "event":
                    update_results[fact.character]["events_updated"] = True
                    update_results[fact.character]["events_count"] += 1
                else:
                    update_results[fact.character]["profile_updated"] = True
                    update_results[fact.character]["profile_count"] += 1

            return {
                "success": True,
                "ingest_mode": effective_mode,
                "session_date": session_date,
                "characters_processed": characters,
                "update_results": update_results,
            }
        except Exception as exc:
            logger.error("update_character_memory 失败: %s", exc)
            return {
                "success": False,
                "error": str(exc),
                "session_date": session_date,
                "characters_processed": characters,
                "update_results": {},
            }

    def clear_character_memory(self, characters: list[str]) -> dict[str, Any]:
        results = {}
        for character in characters:
            try:
                cur = self._store._db.execute(
                    """DELETE FROM memory_items
                       WHERE json_extract(extra_json,'$.scope_channel')=?
                         AND json_extract(extra_json,'$.scope_chat_id')=?""",
                    (SCOPE_CHANNEL, character),
                )
                self._store._db.commit()
                results[character] = {"success": True, "deleted": cur.rowcount}
            except Exception as exc:
                logger.error("clear_character_memory 失败 character=%s: %s", character, exc)
                results[character] = {"success": False, "error": str(exc)}
        return {"success": True, "results": results}
