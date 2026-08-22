from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DatabaseConfig:
    """PostgreSQL connection configuration."""

    url: str = "postgresql+asyncpg://localhost:5432/nexus"
    pool_size: int = 20
    max_overflow: int = 10
    echo: bool = False
