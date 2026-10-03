"""persona 模板管理 CLI（c9-persona-relationship task 2.4，管理员应急/自动化入口）。

用法：

    python scripts/persona_admin.py template-create --name "温柔学姐" \\
        --identity-file id.txt --rules-file rules.txt --self-file self.txt
    python scripts/persona_admin.py template-list [--all]
    python scripts/persona_admin.py template-disable --id <uuid>

模板的修改/停用只影响后续 onboarding，已提交 tenant 快照不受影响
（c9-persona-relationship spec「模板变更不覆盖已有 tenant」）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import load_config
from bootstrap.db.config import DatabaseConfig
from bootstrap.db.engine import create_engine, create_session_factory
from bootstrap.work_queue import async_pg_url


def _read_or_inline(value: str | None, file_value: str | None, label: str) -> str:
    if file_value:
        return Path(file_value).read_text(encoding="utf-8")
    if value:
        return value
    raise SystemExit(f"缺少 {label}（--{label} 或 --{label}-file）")


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("template-create")
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--identity")
    p_create.add_argument("--identity-file")
    p_create.add_argument("--rules")
    p_create.add_argument("--rules-file")
    p_create.add_argument("--self")
    p_create.add_argument("--self-file")

    p_list = sub.add_parser("template-list")
    p_list.add_argument("--all", action="store_true", help="含停用模板")

    p_disable = sub.add_parser("template-disable")
    p_disable.add_argument("--id", required=True)

    args = parser.parse_args()

    config = load_config(args.config)
    if config.storage.backend != "postgres":
        print(json.dumps({"error": "storage.backend != postgres"}))
        return 2
    engine = create_engine(
        DatabaseConfig(
            url=async_pg_url(config.storage.postgres_url),
            pool_size=config.storage.pool_size,
        )
    )
    session_factory = create_session_factory(engine)
    from bootstrap.db.repository.persona_repo import PersonaRepository

    repo = PersonaRepository(session_factory)
    try:
        if args.cmd == "template-create":
            row = await repo.create_template(
                name=args.name,
                identity=_read_or_inline(args.identity, args.identity_file, "identity"),
                personality_rules=_read_or_inline(args.rules, args.rules_file, "rules"),
                self_model=_read_or_inline(args.self, args.self_file, "self"),
            )
            print(json.dumps({"id": row["id"], "name": row["name"]}, ensure_ascii=False))
            return 0
        if args.cmd == "template-list":
            rows = await repo.list_templates(enabled_only=not args.all)
            print(
                json.dumps(
                    [
                        {"id": r["id"], "name": r["name"], "enabled": r["enabled"]}
                        for r in rows
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.cmd == "template-disable":
            ok = await repo.set_template_enabled(args.id, enabled=False)
            print(json.dumps({"id": args.id, "disabled": ok}))
            return 0 if ok else 1
        return 2
    finally:
        await engine.dispose()


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
