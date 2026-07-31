"""LoCoMo dataset loader.

Reads locomo10.json from snap-research/locomo and produces structured
Conversation objects with parsed sessions + QA pairs.

Data format (locomo10.json):
  Top-level: list of conversation dicts.
  Each conversation:
    sample_id: str
    conversation:
      speaker_a: str
      speaker_b: str
      session_1: list of turns
      session_1_date_time: str
      ...
    qa: list of {question, answer, category, evidence, ...}
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from eval.longmemeval.dataset import LMETurn

logger = logging.getLogger(__name__)

CATEGORY_MAP = {
    1: "single_hop",
    2: "temporal",
    3: "multi_hop",
    4: "open_domain",
    5: "adversarial",
}

SUPPORTED_QUESTION_TYPES = tuple(CATEGORY_MAP.values())


@dataclass
class LoCoMoQA:
    """A single QA pair from LoCoMo."""

    question: str
    answer: str
    category: int
    category_name: str
    evidence: list[str]
    qa_index: int
    is_adversarial: bool = False


@dataclass
class LoCoMoConversation:
    """One LoCoMo conversation with its sessions and QA pairs."""

    sample_id: str
    speaker_a: str
    speaker_b: str
    sessions: list[list[LMETurn]]  # haystack-ready sessions
    session_dates: list[str]
    qa_pairs: list[LoCoMoQA]
    total_turns: int = 0

    @property
    def merged_session_key(self) -> str:
        return f"locomo:{self.sample_id}"


def _parse_sessions(
    raw_conv: dict,
    speaker_a_name: str = "speaker_a",
    speaker_b_name: str = "speaker_b",
) -> tuple[list[list[LMETurn]], list[str], int]:
    """Extract sessions and dates from a LoCoMo conversation dict.

    Sessions are stored as session_N / session_N_date_time keys.
    ``speaker_a_name`` / ``speaker_b_name`` are the actual character names
    (e.g. "Caroline", "Melanie") used in the ``speaker`` field of each turn.

    Returns (sessions, dates, total_turns).
    """
    sessions: list[list[LMETurn]] = []
    dates: list[str] = []
    total_turns = 0

    for i in range(1, 100):
        session_key = f"session_{i}"
        date_key = f"session_{i}_date_time"

        if session_key not in raw_conv:
            break

        raw_turns = raw_conv[session_key]
        if not raw_turns:
            continue

        parsed: list[LMETurn] = []
        for turn in raw_turns:
            speaker = str(turn.get("speaker", "") or "")
            text = str(turn.get("text", "") or "").strip()
            if not text:
                continue

            # Map speaker to user/assistant role using the actual character names
            if speaker == speaker_a_name:
                role = "user"
            elif speaker == speaker_b_name:
                role = "assistant"
            else:
                logger.warning("unknown speaker %s in %s", speaker, session_key)
                continue

            parsed.append(LMETurn(role=role, content=text))

        if parsed:
            # Ensure alternating pattern: if starts with assistant, prepend a user turn
            # This is needed because Nexus ingest expects user-first sessions
            sessions.append(parsed)
            total_turns += len(parsed)

        date_raw = raw_conv.get(date_key, "")
        dates.append(str(date_raw or ""))

    return sessions, dates, total_turns


def _parse_qa(raw_qa: list[dict]) -> list[LoCoMoQA]:
    """Extract QA pairs from a LoCoMo conversation."""
    qa_pairs: list[LoCoMoQA] = []
    for idx, item in enumerate(raw_qa):
        category = int(item.get("category", 0))
        category_name = CATEGORY_MAP.get(category, "unknown")
        answer = str(item.get("answer", "") or "").strip()

        qa_pairs.append(
            LoCoMoQA(
                question=str(item.get("question", "") or "").strip(),
                answer=answer,
                category=category,
                category_name=category_name,
                evidence=[str(e) for e in (item.get("evidence") or [])],
                qa_index=idx,
                is_adversarial=(category == 5),
            )
        )
    return qa_pairs


def load_locomo(path: Path | str) -> list[LoCoMoConversation]:
    """Load locomo10.json and return parsed conversations."""
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(raw, list):
        raise ValueError(f"Expected JSON array, got {type(raw)}")

    conversations: list[LoCoMoConversation] = []
    for item in raw:
        sample_id = str(item.get("sample_id", ""))
        conv_data = item.get("conversation", {})
        raw_qa = item.get("qa", [])

        speaker_a_name = str(conv_data.get("speaker_a", "") or "speaker_a")
        speaker_b_name = str(conv_data.get("speaker_b", "") or "speaker_b")
        sessions, dates, total_turns = _parse_sessions(
            conv_data,
            speaker_a_name=speaker_a_name,
            speaker_b_name=speaker_b_name,
        )
        qa_pairs = _parse_qa(raw_qa)

        conversations.append(
            LoCoMoConversation(
                sample_id=sample_id,
                speaker_a=str(conv_data.get("speaker_a", "")),
                speaker_b=str(conv_data.get("speaker_b", "")),
                sessions=sessions,
                session_dates=dates,
                qa_pairs=qa_pairs,
                total_turns=total_turns,
            )
        )

    logger.info(
        "Loaded %d conversations (%d total QA pairs)",
        len(conversations),
        sum(len(c.qa_pairs) for c in conversations),
    )
    return conversations
