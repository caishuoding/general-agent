"""验证工具由模型自主选择，而不是由应用意图正则触发。"""

from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

from general_agent.memory.correction import MemoryCorrection
from general_agent.memory.tools import MemoryTools, memory_tools


def test_agent_tools_cannot_bypass_explicit_search_authorization() -> None:
    """验证模型工具集中不包含联网能力，明确联网统一由对话图的授权节点处理。"""
    names = {item['function']['name'] for item in memory_tools()}

    assert 'web_search' not in names
    assert {'add_memory', 'search_chat_history', 'search_memories', 'update_memory', 'delete_memory'} == names


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

    result = MemoryTools(memory, MagicMock(), MagicMock()).execute(
        checkpoint,
        'update_memory',
        {'memory_id': str(action_id), 'version': 1, 'fact': '不喜欢吃黄瓜'},
        MagicMock(),
    )

    assert result == {'status': 'complete', 'memory_id': str(action_id), 'action': 'updated'}
    cast(MagicMock, MemoryCorrection.apply).assert_called_once()
