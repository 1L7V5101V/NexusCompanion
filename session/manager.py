import json
import logging
import re
from dataclasses import field, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# 保留完整 tool_result 的最近轮次数；更早的轮次仅保留调用结构，结果替换为占位符
_RECENT_TOOL_ROUNDS = 3
_CLEARED = "[已清除]"
_INFERENCE_TAG = "[以下为推演内容，本轮未调用工具，不可作为事实依据]\n"


def _safe_filename(key: str) -> str:
    """Convert a session key to a safe filename."""
    return re.sub(r'[^\w\-]', '_', key)


@dataclass
class Session:
    """
    单次对话中的session,用JSONL格式储存。
    消息是append-only的。
    """
    key: str # channel:chat_id
    messages : list[dict[str,Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()


    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """将 session 消息展开为 LLM 可直接使用的 OpenAI 格式消息列表。

        assistant 消息中的 tool_chain 会被展开为：
          assistant(tool_calls) → tool(result) → ... → assistant(final_text)

        近期 _RECENT_TOOL_ROUNDS 个 assistant 轮次保留完整 tool_result；
        更早的轮次将 tool_result 内容替换为占位符，节省 token 同时保留因果结构。
        """
        messages = self.messages[-max_messages:]

        # 找到"近期边界"：倒数第 _RECENT_TOOL_ROUNDS 个 assistant 消息的索引
        assistant_indices = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
        if len(assistant_indices) > _RECENT_TOOL_ROUNDS:
            recent_boundary = assistant_indices[-_RECENT_TOOL_ROUNDS]
        else:
            recent_boundary = 0  # 全部视为近期

        out: list[dict[str, Any]] = []
        for i, m in enumerate(messages):
            role = m.get("role")
            is_recent = i >= recent_boundary

            if role == "user":
                out.append({"role": "user", "content": m.get("content", "")})

            elif role == "assistant":
                tool_chain: list[dict] = m.get("tool_chain") or []

                # 展开每个迭代组：assistant(tool_calls) + tool(results)
                for group in tool_chain:
                    calls: list[dict] = group.get("calls") or []
                    if not calls:
                        continue
                    out.append({
                        "role": "assistant",
                        "content": group.get("text"),  # 可能为 None
                        "tool_calls": [
                            {
                                "id": c["call_id"],
                                "type": "function",
                                "function": {
                                    "name": c["name"],
                                    "arguments": json.dumps(
                                        c.get("arguments", {}), ensure_ascii=False
                                    ),
                                },
                            }
                            for c in calls
                        ],
                    })
                    for c in calls:
                        out.append({
                            "role": "tool",
                            "tool_call_id": c["call_id"],
                            "content": c["result"] if is_recent else _CLEARED,
                        })

                # 最终文本回复：若该轮没有工具链，标记为推演内容，避免被后续轮次当成事实引用
                content = m.get("content", "") or ""
                if not tool_chain and content and not content.startswith(_INFERENCE_TAG):
                    content = _INFERENCE_TAG + content
                out.append({"role": "assistant", "content": content})

        return out

    def clear(self) -> None:
        self.messages = []
        self.updated_at = datetime.now()
        self.last_consolidated = 0

class SessionManager:
    def __init__(self,workspace: Path):
        self.workspace = workspace
        self.session_dir = workspace / "sessions"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, Session] = {}

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = _safe_filename(key)
        return self.session_dir / f"{safe_key}.jsonl"


    def get_or_create(self,key:str) -> Session:
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key)
        self._cache[key] = session
        return session

    def _load(self,key: str) -> Session:
        path = self._get_session_path(key)
        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            last_consolidated = 0

            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)
            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated,
            )
        except Exception as e:
            logging.warning(f"Failed to load {key}: {e}")
            return None

    def save(self,session: Session) -> None:
        path = self._get_session_path(session.key)
        session.updated_at = datetime.now()

        with open(path, "w") as f:
            metadata_line = {
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "last_consolidated": session.last_consolidated,
                "metadata": session.metadata
            }
            # 先写入元数据
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        self._cache[session.key] = session

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        sessions = []

        for path in self.session_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path) as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            sessions.append({
                                "key": path.stem.replace("_", ":"),
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path)
                            })
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)

    def get_channel_metadata(self, channel: str) -> list[dict[str, Any]]:
        """返回指定 channel 的所有 session 的 metadata（只读首行，不加载消息）。

        返回列表元素形如：{"key": "telegram:123456", "chat_id": "123456", "metadata": {...}}
        """
        results = []
        prefix = _safe_filename(channel + ":")
        for path in self.session_dir.glob(f"{prefix}*.jsonl"):
            try:
                with open(path) as f:
                    first_line = f.readline().strip()
                if not first_line:
                    continue
                data = json.loads(first_line)
                if data.get("_type") != "metadata":
                    continue
                key = data.get("key") or path.stem.replace("_", ":", 1)
                chat_id = key.split(":", 1)[-1] if ":" in key else path.stem[len(prefix):]
                results.append({
                    "key": key,
                    "chat_id": chat_id,
                    "metadata": data.get("metadata", {}),
                })
            except Exception:
                continue
        return results
