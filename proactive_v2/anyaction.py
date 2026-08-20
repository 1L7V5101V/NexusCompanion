from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class QuotaStore:
    """Stub: 主动推送配额存储。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._state: dict[str, object] = {}
        logger.debug("QuotaStore stub initialized: path=%s", path)
