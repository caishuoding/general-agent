"""经公开聊天接口验证明确记忆动作与会话资料的组合行为。"""

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.memory.test_memory import configure_memory, memory_provider
from tests.conversation.test_streaming import wait_message
