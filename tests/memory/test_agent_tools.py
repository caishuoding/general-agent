"""验证工具由模型自主选择，而不是由应用意图正则触发。"""

from typing import Any, cast
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from general_agent.memory.correction import MemoryCorrection
from general_agent.memory.tools import MemoryTools, agent_tools, memory_tools


def test_time_tool_returns_beijing_date_across_utc_midnight(monkeypatch: Any) -> None:
    """工具执行入口将 UTC 时钟转换成北京时间，日期和星期同步跨日。"""
    clock = MagicMock()
    clock.now.return_value = datetime(2026, 10, 21, 16, 30, 45, tzinfo=timezone.utc)
    monkeypatch.setattr('general_agent.agent.time.datetime', clock)
    result = MemoryTools(MagicMock(), MagicMock()).execute(
        MagicMock(), 'get_current_time', {}, MagicMock(),
    )
    assert result == {
        'datetime': '2026-10-22T00:30:45+08:00',
        'timezone': 'Asia/Shanghai',
        'weekday': '星期四',
    }


def test_time_tool_rejects_timezone_override() -> None:
    """无参数工具拒绝时区覆盖，避免模型改变已约定的北京时间。"""
    with pytest.raises(ValidationError):
        MemoryTools(MagicMock(), MagicMock()).execute(
            MagicMock(), 'get_current_time', {'timezone': 'UTC'}, MagicMock(),
        )


def test_agent_tools_register_web_search_as_a_normal_model_tool() -> None:
    """验证网页、文档、记忆和北京时间工具注册在同一个模型自主调用循环中。"""
    memory_names = {item['function']['name'] for item in memory_tools()}
    names = {item['function']['name'] for item in agent_tools(documents=True)}

    assert {'add_memory', 'search_memories', 'update_memory', 'delete_memory'} == memory_names
    assert names == memory_names | {'search_web', 'fetch_web', 'search_documents', 'read_document', 'get_current_time'}


def test_update_tool_does_not_reparse_user_intent(monkeypatch: Any) -> None:
    """验证 Agent 调用更新工具后，服务端只核对目标和版本，不再解析用户措辞。"""
    action_id = uuid4()
    connection = MagicMock()

    def execute(query: str, parameters: tuple[Any, ...]) -> MagicMock:
        """为目标观察和正式条目返回有效版本，并拒绝任何用户正文查询。"""
        assert 'SELECT content' not in query
        cursor = MagicMock()
        if 'memory_tool_observations' in query:
            cursor.fetchone.return_value = {'version': 1}
        elif 'memory_actions' in query:
            cursor.fetchone.return_value = {'status': 'complete', 'version': 1}
        else:
            raise AssertionError(query)
        return cursor

    connection.execute.side_effect = execute
    memory = MagicMock()
    memory.database.connect.return_value.__enter__.return_value = connection
    checkpoint = MagicMock()
    checkpoint.message_id = uuid4()
    checkpoint.operation = MagicMock()
    checkpoint.key.return_value = 'update-key'
    checkpoint.get.return_value = None
    monkeypatch.setattr(MemoryCorrection, 'apply', MagicMock(return_value={'status': 'complete'}))

    result = MemoryTools(memory, MagicMock()).execute(
        checkpoint,
        'update_memory',
        {'memory_id': str(action_id), 'version': 1, 'fact': '不喜欢吃黄瓜'},
        MagicMock(),
    )

    assert result == {'status': 'complete', 'memory_id': str(action_id), 'action': 'updated'}
    cast(MagicMock, MemoryCorrection.apply).assert_called_once()
