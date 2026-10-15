"""必要的 Mem0 自定义适配契约：真实 SDK、PostgreSQL 与受控外部 HTTP。"""

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from general_agent.app import create_app
from general_agent.database import Database
from general_agent.embedding import EmbeddingConfiguration
from general_agent.memory import MemoryService
from general_agent.settings import EmbeddingSettingsStore
from test_memory import configure_memory


def test_mem0_pg_lifecycle_score_metadata_and_no_sqlite(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK CRUD、历史、重启及重置均留在 PG；相似度和更新来源满足应用契约。"""
    def forbid_sqlite(*args: Any, **kwargs: Any) -> None:
        """把任何默认 SQLite 构造变成失败，证明没有先构造再替换的隐藏路径。"""
        raise AssertionError('SQLite is forbidden')
    def provider(request: httpx.Request) -> httpx.Response:
        """返回可区分茶与咖啡的向量，独立验证实际 PG 余弦排序。"""
        payload = json.loads(request.content)
        return httpx.Response(200,json={'model':'embed','data':[{'index':i,'embedding':[1.,0.,0.] if '茶' in text else [0.,1.,0.]} for i,text in enumerate(payload['input'])],'usage':{'prompt_tokens':7,'total_tokens':7}})
    monkeypatch.setattr(sqlite3,'connect',forbid_sqlite)
    app = create_app(database_url,tmp_path,httpx.MockTransport(provider))
    with TestClient(app,base_url='http://127.0.0.1:8000') as client:
        configure_memory(client)
        database = Database(database_url)
        embedding = EmbeddingConfiguration(database,EmbeddingSettingsStore(tmp_path))
        service = MemoryService(database,embedding,app.state.calls,tmp_path)
        settings = embedding.verified()
        with app.state.budget.operation('adapter_contract',['lifecycle']) as (context,_):
            with service.session(settings,context) as memory:
                tea = memory.add('我喜欢茶',user_id='local-person',metadata={'source_message_id':'known-source'},infer=False)['results'][0]['id']
                coffee = memory.add('朋友喜欢咖啡',user_id='someone-else',infer=False)['results'][0]['id']
                assert memory.get(tea)['memory'] == '我喜欢茶'
                hits = memory.search('茶',user_id='local-person',threshold=.9)['results']
                assert [item['id'] for item in hits] == [tea] and hits[0]['score'] == 1.
                memory.update(tea,data='我每天喝茶')
                assert memory.get(tea)['metadata']['source_message_id'] == 'known-source'
                assert [(item['event'],item['new_memory']) for item in memory.history(tea)] == [('ADD','我喜欢茶'),('UPDATE','我每天喝茶')]
                pool = memory.pool
            assert pool.closed
            memory.close()
            with service.session(settings,context) as restarted:
                assert restarted.get(tea)['memory'] == '我每天喝茶'
                assert len(restarted.history(tea)) == 2
                restarted.delete(tea)
                restarted.db.purge(tea)
                assert restarted.get(tea) is None and restarted.history(tea) == []
                assert restarted.get(coffee)['memory'] == '朋友喜欢咖啡'
                restarted.reset()
                assert restarted.get_all(user_id='someone-else')['results'] == []
                assert restarted.history(coffee) == []
                after = restarted.add('重置后喜欢茶',user_id='local-person',infer=False)['results'][0]['id']
                assert restarted.get(after)['memory'] == '重置后喜欢茶'
        assert all(call['unit'] == 'embedding_tokens' for call in client.get('/api/usage').json())
        assert len(client.get('/api/usage').json()) == 6
        assert os.environ['MEM0_TELEMETRY'] == 'False'
        assert not list(tmp_path.rglob('*.db'))
