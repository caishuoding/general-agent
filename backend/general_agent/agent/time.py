"""提供无需联网、固定使用北京时间的系统时钟工具。"""

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from general_agent.prompts import TOOL_DESCRIPTIONS


class CurrentTimeInput(BaseModel):
    """时间工具无需参数，拒绝调用方覆盖时区或传入其他字段。"""

    model_config = ConfigDict(extra='forbid', strict=True)


def current_time_tool() -> dict[str, Any]:
    """返回供模型自主调用的无参数时间工具定义。"""
    return {'type': 'function', 'function': {
        'name': 'get_current_time',
        'description': TOOL_DESCRIPTIONS['get_current_time'],
        'parameters': CurrentTimeInput.model_json_schema(),
    }}


def get_current_time(arguments: dict[str, Any]) -> dict[str, Any]:
    """校验空参数并实时读取系统时钟，返回北京时间、时区和中文星期。"""
    CurrentTimeInput.model_validate(arguments)
    current = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
    return {
        'datetime': current.isoformat(timespec='seconds'),
        'timezone': 'Asia/Shanghai',
        'weekday': ('星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日')[current.weekday()],
    }
