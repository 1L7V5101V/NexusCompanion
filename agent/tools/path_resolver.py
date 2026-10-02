"""C7 task 5.1：租户路径解析器（§5.8.5 / design ADR-5）。

文件类工具 SHALL NOT 接受任意物理路径或由模型/客户端传入的 root——root 由
服务端按「租户 × 资源类别」解析。多租户模式（auth.enabled ∧ storage.backend =
"postgres"，与存储切换同步激活）布局为 ``<workspace>/tenants/<tenant_id>/...``；
单机模式回退工具构造时的既有 allowed_dir（owner 行为不变，ADR-5）。

解析规则：拒绝绝对路径 / ``~`` / ``..`` 逃逸 / 符号链接逃逸；限制目录深度。
大小与单次读取限制由各文件工具既有逻辑承担（§5.8.5 末段）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from agent.tools.context import ToolExecutionContext

_MAX_DEPTH = 12


class PathResolveError(PermissionError):
    """租户路径解析失败（越权/逃逸/非法形态），调用方转为结构化拒绝。"""


def _is_inside(path: Path, root: Path) -> bool:
    try:
        _ = path.relative_to(root)
        return True
    except ValueError:
        return False


class TenantPathResolver:
    """按租户解析资源 root；resolve_relative 做逃逸面校验。"""

    def __init__(self, workspace_root: Path, *, multi_tenant: bool) -> None:
        self._workspace_root = Path(workspace_root)
        self._multi_tenant = multi_tenant

    @property
    def multi_tenant(self) -> bool:
        return self._multi_tenant

    # ── 文件工具根（read_file/list_dir/write_file/edit_file/read_image_vision）──

    def file_root(self, *, tenant_id: str, fallback: Path | None) -> Path | None:
        """多租户模式返回租户工作区根（按需创建）；单机模式回退 fallback。"""
        if not self._multi_tenant or not tenant_id:
            return fallback
        root = self._workspace_root / "tenants" / tenant_dirname(tenant_id) / "workspace"
        root.mkdir(parents=True, exist_ok=True)
        return root

    # ── 资源类别根（§5.8.5 Protocol；随 C6/C14 消费）────────────────────

    def _category_root(self, context: ToolExecutionContext, category: str) -> Path:
        root = (
            self._workspace_root
            / "tenants"
            / tenant_dirname(context.tenant_id)
            / category
            if self._multi_tenant
            else self._workspace_root / category
        )
        root.mkdir(parents=True, exist_ok=True)
        return root

    def attachments_root(self, context: ToolExecutionContext) -> str:
        return str(self._category_root(context, "attachments"))

    def scratch_root(self, context: ToolExecutionContext) -> str:
        return str(self._category_root(context, "scratch"))

    def exports_root(self, context: ToolExecutionContext, job_id: str) -> str:
        base = self._category_root(context, "exports")
        root = base / _safe_segment(job_id)
        root.mkdir(parents=True, exist_ok=True)
        return str(root)

    def mcp_root(self, context: ToolExecutionContext, mcp_id: str) -> str:
        base = self._category_root(context, "mcp")
        root = base / _safe_segment(mcp_id)
        root.mkdir(parents=True, exist_ok=True)
        return str(root)

    # ── 逃逸面校验 ─────────────────────────────────────────────────────

    def resolve_relative(self, root: str | Path, path: str) -> Path:
        """相对逻辑路径 → 校验后的物理路径；任何逃逸形态拒绝。"""
        if not path or not path.strip():
            raise PathResolveError("路径为空")
        raw = Path(path)
        if raw.is_absolute() or path.startswith("~"):
            raise PathResolveError(f"拒绝绝对路径：{path}")
        parts = [part for part in raw.parts if part not in (".",)]
        if any(part == ".." for part in parts):
            raise PathResolveError(f"拒绝包含 .. 的路径：{path}")
        if len(parts) > _MAX_DEPTH:
            raise PathResolveError(f"路径层级超限（>{_MAX_DEPTH}）：{path}")
        root_path = Path(root).resolve()
        resolved = (root_path / Path(*parts)).resolve()
        if not _is_inside(resolved, root_path):
            raise PathResolveError(f"路径越出资源根：{path}")
        return resolved


def tenant_dirname(tenant_id: str) -> str:
    """tenant_id → 目录名：清洗非法字符（Windows 不允许 ":"）并附短哈希保唯一。"""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", tenant_id)[:48]
    digest = hashlib.sha1(tenant_id.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned}-{digest}"


def _safe_segment(segment: str) -> str:
    if any(ch in segment for ch in ("/", "\\")) or ".." in segment or not segment:
        raise PathResolveError(f"非法资源段：{segment!r}")
    cleaned = "".join(ch for ch in segment if ch.isalnum() or ch in "-_")
    if not cleaned or cleaned.startswith("."):
        raise PathResolveError(f"非法资源段：{segment!r}")
    return cleaned[:64]
