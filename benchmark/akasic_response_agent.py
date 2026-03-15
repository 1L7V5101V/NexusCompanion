from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from benchmark.config_loader import BenchmarkComponents, SCOPE_CHANNEL

logger = logging.getLogger(__name__)

class AkasicResponseAgent:
    def __init__(
        self,
        components: BenchmarkComponents,
        top_k_override: int | None = None,
    ) -> None:
        self._store = components.store
        self._retriever = components.retriever
        self._llm = components.llm_client
        self._model = components.model
        self._top_k = max(1, int(top_k_override or components.retriever._top_k))

        self._prompt_dir = Path(__file__).parent / "prompts"
        self._generate_answer_prompt = ""
        prompt_path = self._prompt_dir / "generate_answer.txt"
        if prompt_path.exists():
            self._generate_answer_prompt = prompt_path.read_text(encoding="utf-8")

    def _call_llm(self, prompt: str, max_tokens: int = 1000, temperature: float = 0.1) -> str:
        try:
            response = self._llm.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.error("LLM 调用失败: %s", exc)
            return ""

    async def _retrieve_async(self, question: str, characters: list[str]) -> list[dict]:
        all_items: list[dict] = []
        for character in characters:
            items = await self._retriever.retrieve(
                query=question,
                memory_types=["event", "profile"],
                top_k=self._top_k,
                scope_channel=SCOPE_CHANNEL,
                scope_chat_id=character,
                require_scope_match=True,
            )
            all_items.extend(items)
        all_items.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
        return all_items[: self._top_k]

    @staticmethod
    def _format_retrieved_events(items: list[dict]) -> list[dict]:
        events: list[dict] = []
        rank = 1
        for item in items:
            if item.get("memory_type") != "event":
                continue
            extra = item.get("extra_json") or {}
            events.append(
                {
                    "character": extra.get("scope_chat_id", ""),
                    "event": item.get("summary", ""),
                    "score": item.get("score", 0.0),
                    "rank": rank,
                }
            )
            rank += 1
        return events

    def _generate_answer(
        self, question: str, context_text: str, character_profile: str = ""
    ) -> str:
        if self._generate_answer_prompt:
            prompt = self._generate_answer_prompt.format(
                question=question,
                context_text=context_text,
                character_profile=character_profile,
            )
        else:
            prompt = (
                f"Based on the following context, answer the question concisely.\n\n"
                f"Context:\n{context_text}\n\n"
                f"Question: {question}\n\n"
                f"Answer (≤20 words):"
            )

        raw = self._call_llm(prompt, max_tokens=500, temperature=0.1)

        match = re.search(r"<result>(.*?)</result>", raw, re.DOTALL)
        if match:
            return match.group(1).strip()
        return raw.strip()

    def answer_question(
        self,
        question: str,
        characters: list[str] | None = None,
        max_iterations: int = 1,
    ) -> dict[str, Any]:
        try:
            characters = characters or []
            items = asyncio.run(self._retrieve_async(question, characters))
            context_text, selected_ids = self._retriever.build_injection_block(items)
            selected_items = [
                item for item in items if str(item.get("id", "")) in set(selected_ids)
            ]
            if not context_text:
                context_text = "\n".join(item.get("summary", "") for item in items)
            retrieved_events = self._format_retrieved_events(selected_items or items)
            answer = self._generate_answer(question, context_text)
            return {
                "success": True,
                "answer": answer,
                "retrieved_content": context_text,
                "retrieved_events": retrieved_events,
                "characters_searched": characters,
                "total_retrieved": len(items),
            }
        except Exception as exc:
            logger.error("answer_question 失败: %s", exc)
            return {
                "success": False,
                "error": str(exc),
                "answer": "",
                "retrieved_content": "",
                "retrieved_events": [],
                "characters_searched": characters or [],
                "total_retrieved": 0,
            }

    def search_character_events(
        self,
        query: str,
        characters: list[str],
        top_k: int = 10,
    ) -> dict[str, Any]:
        all_items = asyncio.run(self._retrieve_async(query, characters))
        event_items = [item for item in all_items if item.get("memory_type") == "event"]
        return {
            "success": True,
            "combined_results": [
                {
                    "character": (i.get("extra_json") or {}).get("scope_chat_id", ""),
                    "event": i["summary"],
                    "score": i["score"],
                    "rank": idx + 1,
                }
                for idx, i in enumerate(event_items[:top_k])
            ],
        }

    def get_character_profile(self, character_name: str) -> dict[str, Any]:
        try:
            rows = self._store._db.execute(
                """SELECT summary FROM memory_items
                   WHERE memory_type='profile' AND status='active'
                     AND json_extract(extra_json,'$.scope_channel')=?
                     AND json_extract(extra_json,'$.scope_chat_id')=?
                   ORDER BY updated_at DESC LIMIT 10""",
                (SCOPE_CHANNEL, character_name),
            ).fetchall()
            profile = "\n\n".join(r[0] for r in rows)
            return {
                "success": True,
                "character_name": character_name,
                "profile": profile,
                "file_exists": bool(profile.strip()),
            }
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "character_name": character_name,
                "profile": "",
                "file_exists": False,
            }
