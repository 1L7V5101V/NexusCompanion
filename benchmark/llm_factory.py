"""
LLM Client Factory for Akasic Benchmark

优先读取项目根目录的 config.json（与主 agent 保持一致），
回退到环境变量。
"""

import os
import sys
from pathlib import Path
from typing import Optional

from memu.llm import OpenAIClient, BaseLLMClient

_HERE = Path(__file__).parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_DEFAULT_MODEL = "qwen-plus"


def _load_config_values():
    """从 config.json 读取 api_key / base_url / model，失败时返回 None。"""
    try:
        from agent.config import load_config
        config = load_config(_PROJECT_ROOT / "config.json")
        return config.api_key, config.base_url, config.model
    except Exception:
        return None, None, None


def create_llm_client(
    chat_deployment: str = None,
    azure_endpoint: Optional[str] = None,
    api_key: Optional[str] = None,
    use_entra_id: bool = False,
    api_version: str = "2024-02-01",
    **kwargs,
) -> BaseLLMClient:
    cfg_key, cfg_base, cfg_model = _load_config_values()

    resolved_key = (
        api_key
        or cfg_key
        or os.getenv("DASHSCOPE_API_KEY")
        or os.getenv("API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    resolved_base = azure_endpoint or cfg_base or os.getenv("LLM_BASE_URL", _DEFAULT_BASE_URL)
    resolved_model = chat_deployment or cfg_model or os.getenv("LLM_MODEL", _DEFAULT_MODEL)

    return OpenAIClient(
        model=resolved_model,
        api_key=resolved_key,
        base_url=resolved_base,
    )
