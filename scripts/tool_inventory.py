"""C7 task 1.1：程序化工具清单（静态装配点 + 动态 engine 注入 + ADR-3 对账）。

用法：``python scripts/tool_inventory.py``；输出
``openspec/evidence/c7-tool-isolation/tool-inventory.{json,md}``。

覆盖三类注册来源：
1. bootstrap/toolsets/*.py 与 bootstrap/tools.py 中的 ``registry.register(..., risk=...)``
   装配点（AST 解析，取 tool 类、risk 标签、always_on）；
2. ``agent/tools/`` 下全部 Tool 子类（name 为类属性时直接读；property 时以
   ``object.__new__`` 空实例读取，不执行 ``__init__``）；
3. memory engine ``tool_profile()`` 经 ``register_memory_meta_tools`` 动态注入的工具
   （对引擎类用 ``cls.__new__(cls)`` 取 profile——tool_profile() 为无状态描述，不触碰
   存储依赖；失败则如实报告，不猜）。
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import pkgutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# design.md ADR-3 白名单（本脚本的对账基准；改动白名单须先改 design）。
ADR3_ALLOWED = {
    "recall_memory", "memorize", "forget_memory",
    "search_messages", "fetch_messages",
    "read_file", "list_dir", "write_file", "edit_file", "read_image_vision",
    "message_push",
    "schedule", "remind", "list_schedules", "cancel_schedule",
    "web_search", "web_fetch",
    "tool_search",
}
ADR3_CLOSED = {
    "shell", "spawn", "spawn_manage", "task_output", "task_stop",
    "load_skill", "mcp_add", "mcp_remove", "mcp_list",
}
# 已知壳类/类别规则覆盖项：不算未决项，单列说明。
KNOWN_SHELL = {
    "memory_signal": "壳类静态占位名（运行时被 spec.name 覆盖），由 engine 类别规则覆盖",
    "peer_agent": "peer 委托工具为动态名 delegate_*（按 PeerAgentRegistry 注册）",
}


@dataclass
class Entry:
    name: str
    source: str  # toolset 装配点 / class 扫描 / engine profile
    tool_class: str = ""
    risk_label: str = ""  # 装配点声明的 risk 标签（现状事实，非 C7 effect 定案）
    always_on: bool | None = None
    engine: str = ""  # 来源 engine（动态注入时）


def scan_toolset_registrations() -> list[Entry]:
    """AST 解析 bootstrap 装配点中的 registry.register(...) 调用。"""
    entries: list[Entry] = []
    targets = list((REPO / "bootstrap" / "toolsets").glob("*.py")) + [
        REPO / "bootstrap" / "tools.py",
        REPO / "agent" / "tools" / "meta" / "register.py",
    ]
    for path in targets:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # 两遍：先收集 局部变量 = XxxTool(...) 赋值，再解析 register(变量)。
        var_cls: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                value = node.value
                if (
                    isinstance(target, ast.Name)
                    and isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                ):
                    var_cls[target.id] = value.func.id
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_register = (
                isinstance(func, ast.Attribute) and func.attr == "register"
            )
            if not is_register or not node.args:
                continue
            first = node.args[0]
            cls = ""
            if isinstance(first, ast.Call) and isinstance(first.func, ast.Name):
                cls = first.func.id
            elif isinstance(first, ast.Name):
                # 变量中转：解析到构造类；解析不到的标注为间接注册。
                cls = var_cls.get(first.id, f"<indirect:{first.id}>")
            if not cls:
                continue
            risk = ""
            always_on = None
            for kw in node.keywords:
                if kw.arg == "risk" and isinstance(kw.value, ast.Constant):
                    risk = str(kw.value.value)
                if kw.arg == "always_on" and isinstance(kw.value, ast.Constant):
                    always_on = bool(kw.value.value)
            entries.append(
                Entry(
                    name=f"<{cls}>",
                    source=f"register@{path.relative_to(REPO)}:{node.lineno}",
                    tool_class=cls,
                    risk_label=risk,
                    always_on=always_on,
                )
            )
    return entries


def _static_tool_name(cls: type) -> str:
    """取 Tool 子类的静态 name：类属性 / 本类或基类 property（空实例读取）。"""
    attr = inspect.getattr_static(cls, "name", None)
    if isinstance(attr, str):
        return attr
    if isinstance(attr, property) and attr.fget is not None:
        try:
            return str(attr.fget(cls.__new__(cls)))  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 —— 清单不猜测
            return f"<unreadable: {exc}>"
    return ""


def scan_tool_classes() -> list[Entry]:
    """扫描工具类定义包（agent.tools + agent.mcp + agent.peer_agent + bootstrap.toolsets）。"""
    from agent.tools.base import Tool

    roots = ["agent.tools", "agent.mcp", "agent.peer_agent", "bootstrap.toolsets"]
    entries: list[Entry] = []
    seen: set[str] = set()
    mods: list[Any] = []
    for root_name in roots:
        try:
            pkg = importlib.import_module(root_name)
        except Exception:  # noqa: BLE001 —— 包缺失不阻断清单
            continue
        mods.append(pkg)
        if hasattr(pkg, "__path__"):
            for m in pkgutil.walk_packages(pkg.__path__, prefix=f"{root_name}."):
                try:
                    mods.append(importlib.import_module(m.name))
                except Exception:  # noqa: BLE001
                    pass
    for mod in mods:
        for attr, cls in vars(mod).items():
            if (
                inspect.isclass(cls)
                and issubclass(cls, Tool)
                and cls is not Tool
                and any(
                    cls.__module__.startswith(root)
                    for root in roots
                )
                and cls.__name__ not in seen
            ):
                seen.add(cls.__name__)
                name = _static_tool_name(cls)
                entries.append(
                    Entry(
                        name=name or f"<{cls.__name__}: no static name>",
                        source=f"class@{cls.__module__}",
                        tool_class=cls.__name__,
                    )
                )
    return entries


def scan_engine_profiles() -> list[Entry]:
    """枚举各 memory engine tool_profile 动态注入/覆写的工具。"""
    entries: list[Entry] = []
    engines = [
        ("default", "plugins.default_memory.engine", "DefaultMemoryEngine"),
        ("rachael", "plugins.rachael.engine", "RachaelMemoryEngine"),
    ]
    for engine_id, mod_name, cls_name in engines:
        try:
            mod = importlib.import_module(mod_name)
            cls = getattr(mod, cls_name)
            profile = cls.__new__(cls).tool_profile()
        except Exception as exc:  # noqa: BLE001 —— 如实报告，不猜
            entries.append(
                Entry(
                    name=f"<profile unavailable: {cls_name}: {exc}>",
                    source="engine-profile",
                    engine=engine_id,
                )
            )
            continue
        for slot, label in (
            (profile.recall, "recall"),
            (profile.memorize, "memorize"),
            (profile.forget, "forget"),
        ):
            if slot is not None and slot.name:
                entries.append(
                    Entry(
                        name=slot.name,
                        source=f"engine-profile:{label}",
                        risk_label=slot.risk,
                        engine=engine_id,
                    )
                )
        for spec in profile.tools:
            entries.append(
                Entry(
                    name=spec.name,
                    source="engine-profile:tools",
                    risk_label=spec.risk,
                    engine=engine_id,
                )
            )
    return entries


def reconcile(entries: list[Entry]) -> dict[str, list[str]]:
    """与 ADR-3 白名单对账：静态 id 对两个清单，engine 注入按类别规则单列。"""
    static_names = {
        e.name
        for e in entries
        if e.source.startswith(("class@", "register@")) and not e.name.startswith("<")
    }
    dynamic_names = {
        e.name
        for e in entries
        if e.source.startswith("engine-profile") and not e.name.startswith("<")
    }
    unknown = sorted(
        name
        for name in static_names - ADR3_ALLOWED - ADR3_CLOSED
        if name not in KNOWN_SHELL
    )
    missing_allowed = sorted(ADR3_ALLOWED - static_names - dynamic_names)
    closed_not_found = sorted(
        name for name in ADR3_CLOSED - static_names if name not in KNOWN_SHELL
    )
    return {
        "未决项（静态注册但不在 ADR-3 两个清单，需回 design 补决策）": unknown,
        "engine 注入工具（按 ADR-3 类别规则放行，随引擎绑定）": sorted(dynamic_names),
        "壳类/动态名（由 ADR-3 类别规则覆盖）": sorted(KNOWN_SHELL),
        "ADR-3 允许但清单中未发现": missing_allowed,
        "ADR-3 关闭但清单中未发现（确认是否真实存在）": closed_not_found,
    }


def main() -> int:
    entries = (
        scan_toolset_registrations() + scan_tool_classes() + scan_engine_profiles()
    )
    diffs = reconcile(entries)
    out_dir = REPO / "openspec" / "evidence" / "c7-tool-isolation"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "entries": [asdict(e) for e in entries],
        "reconciliation": diffs,
    }
    (out_dir / "tool-inventory.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = ["# C7 task 1.1 工具清单（含动态注入）", ""]
    for e in entries:
        flags = []
        if e.risk_label:
            flags.append(f"risk={e.risk_label}")
        if e.always_on:
            flags.append("always_on")
        if e.engine:
            flags.append(f"engine={e.engine}")
        lines.append(
            f"- `{e.name}` — {e.source} ({e.tool_class}"
            + ("; " + ", ".join(flags) if flags else "")
            + ")"
        )
    lines += ["", "## 与 ADR-3 对账", ""]
    for title, items in diffs.items():
        lines.append(f"- {title}: {items if items else '无'}")
    (out_dir / "tool-inventory.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for title, items in diffs.items():
        print(f"{title}: {items if items else '无'}")
    return 0 if not any(diffs.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
