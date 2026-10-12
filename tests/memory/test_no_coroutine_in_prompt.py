"""c18 task 2.5：同步上下文里调异步画像存储，不许把协程对象的字符串表示送进提示。

落实 spec「同步上下文不产生未 await 的协程」。这条测试要证明的不是「写法看起来不对」，
而是它真的产出垃圾并且**不报错**：协程对象是真值，所以 `str(x() or "")` 里的 `or ""`
兜不住空值那一路；`coroutine object` 的字符串表示于是直接进正文。同一条路径上也没有
异常抛出，故障表现为「提示里出现一串看不懂的英文」，不是启动即失败。

本文件在接线完成前预期为红——主动推送那两处今天都会把协程的字符串表示交出去。转绿点在
task 4.5（主动推送改用共用渲染器）；那时本文件的断言对象要从「读取器的返回值」改成
「`render_sections` 组装出的画像段」，而不是删掉了事。

覆盖范围如实交代（本文件断言的是主动推送那条路，另外两条今天只能静态覆盖）：

- 主动推送：`plugins/proactive_flow/prompt.py:149-155` 的三个读取点（self /
  long_term / recent_context）通过真调 `build_runtime_context_message` 一起断言，
  外加 `_read_self_text` 单独一条。
- drift（`plugins/drift_flow/runtime.py:600`）：那行嵌在一个需要完整 tick 上下文的
  async 方法里，无法在不造一堆假依赖的前提下单独触发。由 2.4 的静态扫描覆盖，行为断言
  等 4.6 接上共用渲染器后统一补。
- 唤醒（`plugins/wake_proactive/runtime.py:1024`）：**今天导不进来**——该文件
  `:48` 无条件 `from session.embedding_store import MessageEmbeddingStore`，而
  `session/embedding_store.py` 在本仓库任何分支上都不存在。所以这条只能等那个缺陷
  处理完再补行为断言；它的读取点由 2.4 的静态扫描覆盖（裸标识符模式在 `:1025` 逮到）。
  这一条影响 task 4.7 与 spec「四条组装路径」的第四条，已上报待决，不在测试里用
  `object.__new__` 之类的把戏绕过去——那只会让测试看起来覆盖了，实际测的是拼出来的假对象。

`-W error` 本身不足以抓住这个问题：「coroutine was never awaited」是在协程对象被垃圾
回收时才发出的，不是在调用点。所以本文件显式 `gc.collect()` 逼它落地再检查。
"""

from __future__ import annotations

import gc
import warnings
from types import SimpleNamespace
from typing import Callable, cast

import pytest

from core.memory.markdown import MemoryProfileApi
from plugins.proactive_flow.prompt import ProactivePromptBuilder, _read_self_text
from plugins.default_proactive.context import AgentTickContext
from plugins.default_proactive.gateway import GatewayResult
from proactive_v2.config import ProactiveConfig


class AsyncProfileStore:
    """按真实形态建模：PG 侧的画像存储全部是 async 方法。

    `agent/memory_pg.py` 的 `PgMemoryMarkdownStore` 就是这样（read_long_term:62、
    read_self:135、read_recent_context:127 全为 async def），而 `PgMemoryMarkdownStore`
    一旦接上就会撞上同一个错位。
    """

    def __init__(self) -> None:
        self.calls = 0

    async def read_long_term(self) -> str:
        self.calls += 1
        return "该租户的长期画像正文"

    async def read_self(self) -> str:
        self.calls += 1
        return "该租户的自我认知正文"

    async def read_recent_context(self) -> str:
        self.calls += 1
        return "该租户的近期摘要正文"


def _record_call(reader: Callable[[], object]) -> tuple[str, list[str]]:
    """跑一次读取，返回它拿到的文本 + 期间发出的「未 await」告警。"""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        text = str(reader() or "")  # 三条旁路共用的写法
        del text
        gc.collect()  # 告警在协程对象被回收时才发出，不在调用点
        text = str(reader() or "")
    bad = [str(w.message) for w in caught if "never awaited" in str(w.message)]
    return text, bad


def _assert_clean(label: str, text: str, coroutine_warnings: list[str]) -> None:
    assert "coroutine" not in text, (
        f"{label} 把未 await 的协程写进了提示正文：{text!r}"
    )
    assert not coroutine_warnings, f"{label} 留下了未 await 的协程：{coroutine_warnings}"


def _as_profile_api(store: AsyncProfileStore) -> MemoryProfileApi:
    """刻意把不满足协议的 async 存储交给声明了协议的读取点。

    这不是绕类型检查：`MemoryProfileApi`（core/memory/markdown.py:61-80）声明全同步，
    而 PG 侧的存储实现全 async——把后者接上前去就是这条 cast 表达的形状。协议一致性
    检查从来没跑过（`PgMemoryMarkdownStore` 十个方法只实现六个），所以静态上没人拦。
    """
    return cast(MemoryProfileApi, store)


def _builder(store: AsyncProfileStore) -> ProactivePromptBuilder:
    return ProactivePromptBuilder(
        cfg=cast(ProactiveConfig, SimpleNamespace()),
        memory=_as_profile_api(store),
        workspace_context_fn=lambda: "",
    )


def test_proactive_self_reader() -> None:
    text, bad = _record_call(
        lambda: _read_self_text(_as_profile_api(AsyncProfileStore()))
    )
    _assert_clean("主动推送 _read_self_text", text, bad)
    assert text == "该租户的自我认知正文", f"主动推送 _read_self_text 拿到的不是画像正文：{text!r}"


def test_proactive_runtime_context_frame() -> None:
    """真去调 `build_runtime_context_message`——协程字符串正是从这里进提示的。

    覆盖 `prompt.py:149-155` 的三个读取点（self / long_term / recent_context）与
    `:159-161` 的三个段名，断言的是组装完的那一条消息正文。
    """
    builder = _builder(AsyncProfileStore())
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        message = builder.build_runtime_context_message(
            AgentTickContext(), GatewayResult()
        )
        gc.collect()
    body = message["content"]
    _assert_clean(
        "主动推送 runtime_context 帧",
        body,
        [str(w.message) for w in caught if "never awaited" in str(w.message)],
    )
    for expected in ("该租户的自我认知正文", "该租户的长期画像正文", "该租户的近期摘要正文"):
        assert expected in body, f"画像内容没进帧（静默失效）：缺 {expected!r}"


def test_async_store_methods_are_actually_coroutines() -> None:
    """前提自检：本文件建模的存储必须是 async 的，否则上面三条全在测空气。"""
    store = AsyncProfileStore()
    result = store.read_long_term()
    assert hasattr(result, "__await__"), "假存储的方法不是协程，建模失效"
    result.close()
    with pytest.warns(RuntimeWarning, match="never awaited"):
        orphan = store.read_self()
        del orphan
        gc.collect()
