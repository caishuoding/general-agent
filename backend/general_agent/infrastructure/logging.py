"""配置智能体运行日志，专门记录 LangGraph 节点流转和失败原因。"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_agent_logging(directory: Path) -> Path:
    """在配置目录写入可滚动的 agent.log，并返回实际日志路径。"""
    log_directory = directory / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    target = (log_directory / "agent.log").resolve()
    logger = logging.getLogger("general_agent")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for existing in list(logger.handlers):
        if isinstance(existing, RotatingFileHandler):
            if Path(existing.baseFilename).resolve() == target:
                return target
            logger.removeHandler(existing)
            existing.close()
    handler = RotatingFileHandler(target, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.info("event=logging_initialized path=%s", target)
    return target
