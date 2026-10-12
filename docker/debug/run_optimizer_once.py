"""
调试脚本：对沙盒 workspace 里某个租户的画像专表跑一次 MemoryOptimizer，
观察合并后的长期画像与自我认知长什么样。

c18 task 6.11 之后优化器读写的是 `tenant_memory_profile_*` 两张专表，不再碰
MEMORY.md / PENDING.md，所以这里的 before/after 读的也是表。归属为空或解析不出租户时
优化器 fail-closed 跳过（task 4.3）——脚本因此显式收 `--tenant`，默认单机的固定租户。

用法（在容器内）：
  python /app/docker/debug/run_optimizer_once.py \
    --config /sandbox/config.toml \
    --workspace /sandbox/workspace \
    --tenant default
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.config_models import Config
from agent.memory import MemoryStore
from bootstrap.profile_access import build_profile_access
from bootstrap.providers import build_providers
from infra.storage.tenancy import DEFAULT_TENANT
from proactive_v2.memory_optimizer import MemoryOptimizer


async def main(config_path: str, workspace_str: str, tenant_id: str) -> None:
    workspace = Path(workspace_str)
    config = Config.load(config_path)
    provider, _, _ = build_providers(config)
    access = build_profile_access(
        config, memory_db_path=workspace / "memory" / "memory2.db"
    )
    store = access.resolve(tenant_id)

    print(f"=== tenant={tenant_id} 的 long_term（before）===")
    print(await store.read_section("long_term") or "(empty)")
    print("\n=== pending（before）===")
    print(await store.read_entries("pending") or "(empty)")
    print("\n>>> 开始 optimize...\n")

    # SELF 这一路今天仍走文件（relationship_io 未注入时），所以文件 store 还要交一个。
    file_store = MemoryStore(workspace)
    optimizer = MemoryOptimizer(
        memory=file_store,
        provider=provider,
        model=config.model,
        max_tokens=8192,
        profile_stores=access.resolve,
    )
    await optimizer.optimize(tenant_id)

    print("=== long_term（after）===")
    print(await store.read_section("long_term") or "(empty)")
    print("\n=== pending（after，合并吃进去的行应已被取走）===")
    print(await store.read_entries("pending") or "(empty)")
    print("\n=== SELF.md（after，仍是文件那一路）===")
    print(file_store.read_self())

    access.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/sandbox/config.toml")
    parser.add_argument("--workspace", default="/sandbox/workspace")
    parser.add_argument("--tenant", default=DEFAULT_TENANT)
    args = parser.parse_args()
    asyncio.run(main(args.config, args.workspace, args.tenant))
