"""显式 tenant mapping 强制（D3）。

迁移要求一个显式 mapping JSON：把「源身份（通道 / memory）」映射到目标
``tenant_id``。未提供 mapping 或存在未映射源身份时，预检终止、不写任何数据；
单用户 workspace 也必须显式指定目标 tenant，不默认 ``default``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.migrate.config import MEMORY_SOURCE_IDENTITY


class MappingError(Exception):
    """mapping 缺失 / 未映射源身份。"""

    def __init__(self, message: str, unmapped: list[str] | None = None) -> None:
        super().__init__(message)
        self.unmapped = unmapped or []


class TenantMapping:
    """源身份 → tenant_id 的显式映射。"""

    def __init__(self, mapping: dict[str, str]) -> None:
        # 源身份统一去空格；空值视为无效。
        self._mapping = {k.strip(): v for k, v in mapping.items() if k and v}

    @classmethod
    def load(cls, path: Path) -> "TenantMapping":
        if not path.exists():
            raise MappingError(f"tenant mapping 文件不存在: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MappingError(f"tenant mapping JSON 解析失败: {exc}") from exc
        if not isinstance(data, dict):
            raise MappingError("tenant mapping 必须是 <source_identity>: <tenant_id> 对象")
        return cls({str(k): str(v) for k, v in data.items()})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TenantMapping":
        return cls({str(k): str(v) for k, v in data.items()})

    def resolve(self, identity: str) -> str:
        identity = identity.strip()
        if identity not in self._mapping:
            raise MappingError(
                f"未映射源身份: {identity!r}", unmapped=[identity]
            )
        return self._mapping[identity]

    def has(self, identity: str) -> bool:
        return identity.strip() in self._mapping

    def unmapped(self, identities: list[str]) -> list[str]:
        """返回不在 mapping 中的源身份（按输入顺序去重）。"""
        seen: set[str] = set()
        result: list[str] = []
        for ident in identities:
            key = ident.strip()
            if key not in self._mapping and key not in seen:
                seen.add(key)
                result.append(key)
        return result

    def tenants(self) -> set[str]:
        return set(self._mapping.values())

    def to_dict(self) -> dict[str, str]:
        return dict(self._mapping)

    def require_covered(self, identities: list[str]) -> None:
        """预检：存在未映射源身份时抛 MappingError，不写任何数据。"""
        missing = self.unmapped(identities)
        if missing:
            raise MappingError(
                "存在未映射的源身份，拒绝导入（dry-run 可列出）: "
                + ", ".join(repr(m) for m in missing),
                unmapped=missing,
            )
