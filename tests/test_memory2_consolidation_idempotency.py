import asyncio

from memory2.memorizer import Memorizer
from memory2.store import MemoryStore2


class _FakeEmbedder:
    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def test_save_from_consolidation_skips_duplicate_source_ref(tmp_path):
    store = MemoryStore2(tmp_path / "memory2.db")
    memorizer = Memorizer(store, _FakeEmbedder())

    async def _run() -> None:
        await memorizer.save_from_consolidation(
            history_entry="[2026-03-08 12:00] second with different text",
            behavior_updates=[],
            source_ref="session@1-10",
            scope_channel="telegram",
            scope_chat_id="123",
        )
        await memorizer.save_from_consolidation(
            history_entry="[2026-03-08 12:00] first",
            behavior_updates=[],
            source_ref="session@1-10",
            scope_channel="telegram",
            scope_chat_id="123",
        )

    asyncio.run(_run())

    items = store.list_by_type("event")
    assert len(items) == 1
    assert items[0]["reinforcement"] == 1
