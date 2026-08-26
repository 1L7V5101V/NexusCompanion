"""alembic upgrade 封装：避免 env.py 的 fileConfig 破坏调用方日志配置。

alembic env.py 调用 ``logging.config.fileConfig``，以默认 ``disable_existing_loggers=True``
重配 root，并把未在 alembic.ini 中声明的现有 logger（如 agent.*）全部 disabled。若在
pytest 进程内执行 upgrade，后续 caplog 断言会捕获不到任何记录。此封装在升级前后快照并
恢复全部 logger 状态，把副作用限制在调用内。
"""
from __future__ import annotations

import logging

from alembic import command
from alembic.config import Config


def upgrade_head(cfg: Config) -> None:
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_loggers = {
        name: (lgr.level, lgr.disabled, lgr.propagate, list(lgr.handlers))
        for name, lgr in logging.Logger.manager.loggerDict.items()
        if isinstance(lgr, logging.Logger)
    }
    try:
        command.upgrade(cfg, "head")
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        for name, (level, disabled, propagate, handlers) in saved_loggers.items():
            lgr = logging.Logger.manager.loggerDict.get(name)
            if isinstance(lgr, logging.Logger):
                lgr.setLevel(level)
                lgr.disabled = disabled
                lgr.propagate = propagate
                lgr.handlers[:] = handlers
