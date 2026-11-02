"""验证旧原文存储迁移为自足切片并删除冗余表列。"""

from pathlib import Path
from uuid import uuid4

from general_agent.attachments.knowledge import ChunkPolicy
from general_agent.attachments.tools import DocumentTools
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.tokens import TokenCounter


def test_migration_preserves_chunks_embeddings_positions_and_ttl(database_url: str) -> None:
    """旧库直接迁移元数据且不重切片，删除重复正文及外键，重启仍可按第二页读取同片。"""
    database = Database(database_url)
    directory = Path(__file__).resolve().parents[2] / 'backend/general_agent/infrastructure/migrations'
    identifier, conversation, account_id = uuid4(), uuid4(), uuid4()
    with database.connect() as connection:
        connection.execute('CREATE TABLE schema_migrations (version TEXT PRIMARY KEY)')
        for path in sorted(directory.glob('*.sql')):
            if path.name >= '046_shared_document_chunks.sql':
                break
            connection.execute(path.read_text())
            connection.execute('INSERT INTO schema_migrations(version) VALUES(%s)', (path.name,))
        # 该阶段尚未引入独立管理员表，直接构造旧版普通账号覆盖真实升级路径。
        connection.execute(
            "INSERT INTO users(id,username,password_hash,role,must_change_password) VALUES(%s,'legacy_user','legacy-hash','user',FALSE)",
            (account_id,),
        )
        connection.execute('INSERT INTO conversations(id,title,user_id) VALUES(%s,%s,%s)',
                           (conversation, '迁移验证', account_id))
        connection.execute(
            "INSERT INTO attachments(id,conversation_id,filename,size,status,knowledge_version,parse_info,content) "
            "VALUES(%s,%s,'old.pdf',100,'ready',2,'{\"format\":\"pdf\",\"total_units\":2}', 'duplicated full text')",
            (identifier, conversation),
        )
        connection.execute(
            "INSERT INTO document_units(attachment_id,ordinal,page,paragraph,content,structure) "
            "VALUES(%s,1,1,1,'FIRST.', '{}'),(%s,2,2,1,'SECOND.', '{\"section\":[\"Chapter\"],\"end_page\":2}')",
            (identifier, identifier),
        )
        connection.execute(
            "INSERT INTO document_chunks(attachment_id,ordinal,unit_ordinal,end_unit_ordinal,start_offset,end_offset,"
            "content,terms,embedding,embedding_revision,context_prefix,token_count,token_count_method) "
            "VALUES(%s,1,1,2,0,7,'HEADER FIRST.SECOND.',ARRAY['first','second'],'[1,0]','existing-space','HEADER ',20,'character_estimate')",
            (identifier,),
        )
        before = connection.execute('SELECT expires_at FROM attachments WHERE id=%s', (identifier,)).fetchone()
    database.migrate()
    database.migrate()
    documents = DocumentTools(database, ChunkPolicy())
    result = documents.read(conversation, account_id,
                            {'document_id': str(identifier), 'start_page': 2, 'end_page': 2}, TokenCounter('test'), 10000)
    assert result['content'] == [{'chunk_id': 1, 'content': 'HEADER FIRST.SECOND.'}]
    searched = documents.search(conversation, account_id, {'query': 'FIRST'})['results'][0]
    assert searched['content'] == result['content'][0]['content']
    assert searched['end_structure']['section'] == ['Chapter']
    with database.connect() as connection:
        assert connection.execute('SELECT expires_at FROM attachments WHERE id=%s', (identifier,)).fetchone() == before
        assert connection.execute('SELECT embedding,embedding_revision FROM document_chunks WHERE attachment_id=%s',
                                  (identifier,)).fetchone() == {'embedding': [1, 0], 'embedding_revision': 'existing-space'}
        assert connection.execute("SELECT to_regclass('document_units') AS table_name").fetchone() == {'table_name': None}
        assert connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND "
            "((table_name='document_chunks' AND column_name IN ('unit_ordinal','end_unit_ordinal','start_offset','end_offset')) "
            "OR (table_name='attachments' AND column_name='content'))",
        ).fetchall() == []
