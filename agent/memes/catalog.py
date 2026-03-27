from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


@dataclass
class MemeCategory:
    name: str
    desc: str
    aliases: list[str] = field(default_factory=list)
    enabled: bool = True


class MemeCatalog:
    def __init__(self, memes_dir: Path) -> None:
        self._dir = memes_dir
        self._categories: dict[str, MemeCategory] = {}
        self._manifest_mtime: float = -1.0

    def _load(self) -> None:
        """Load or reload manifest if it has changed on disk."""
        manifest = self._dir / "manifest.json"
        if not manifest.exists():
            self._categories = {}
            self._manifest_mtime = -1.0
            return
        mtime = manifest.stat().st_mtime
        if mtime == self._manifest_mtime:
            return
        self._manifest_mtime = mtime
        self._categories = {}
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            return
        for name, info in (data.get("categories") or {}).items():
            self._categories[name] = MemeCategory(
                name=name,
                desc=info.get("desc", ""),
                aliases=info.get("aliases", []),
                enabled=info.get("enabled", True),
            )

    def get_enabled_categories(self) -> list[MemeCategory]:
        self._load()
        return [c for c in self._categories.values() if c.enabled]

    def pick_image(self, tag: str) -> str | None:
        """Randomly pick an image path from the given category. Returns None if unavailable."""
        self._load()
        tag = tag.lower()
        cat = self._categories.get(tag)
        if cat is None or not cat.enabled:
            return None
        cat_dir = self._dir / tag
        if not cat_dir.is_dir():
            return None
        images = [
            f for f in cat_dir.iterdir() if f.suffix.lower() in _IMAGE_SUFFIXES
        ]
        if not images:
            return None
        return str(random.choice(images))

    def build_prompt_block(self) -> str | None:
        """Build the meme categories section for system prompt injection."""
        cats = self.get_enabled_categories()
        if not cats:
            return None
        names = {cat.name.lower() for cat in cats}
        lines = ["可用表情类别："]
        for cat in cats:
            lines.append(f"- {cat.name}: {cat.desc}")
        lines += [
            "",
            "在和用户情感强烈的对话结尾在回复中插入 <meme:category> 附带表情图，每条最多 1 个。",
            "严肃任务、代码解释、工具结果时不使用。",
            "",
            "<example>",
            "对方说：最喜欢你了 → 回复结尾加 <meme:shy>",
            "对方说：你今天好棒 → 回复结尾加 <meme:shy>",
            "已经用了颜文字、对方直球说喜欢 → 还是加 <meme:shy>",
            "任务完成、对方说谢谢 → 回复结尾加 <meme:happy>",
            "轻松聊天、说了个小笑话 → 回复结尾加 <meme:clever>",
            "被戳穿、说错话后 → 回复结尾加 <meme:awkward>",
            "帮忙查资料、执行了指令 → 不加",
            "</example>",
        ]
        return "\n".join(lines)
