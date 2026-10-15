"""在预览、人物澄清和批准时重新核验附件来源，不信任模型自报位置。"""

from pathlib import Path
from typing import Any
from uuid import UUID

from general_agent.database import Database


def validate_attachment_source(database: Database, directory: Path, candidate: dict[str, Any], require_person: bool = True) -> None:
    """确认附件仍存在、属于原会话且已经发送，文字依据仍与原可信片段一致。"""
    with database.connect() as connection:
        source=connection.execute("SELECT s.*,a.status,a.conversation_id,a.created_at,c.content FROM attachment_memory_candidates s JOIN attachments a ON a.id=s.attachment_id JOIN attachment_chunks c ON c.attachment_id=a.id AND 'A'||c.citation_id::text=s.citation_key WHERE s.candidate_id=%s",(candidate['id'],)).fetchone()
        sent=connection.execute('SELECT 1 FROM message_attachments ma JOIN messages m ON m.id=ma.message_id WHERE ma.attachment_id=%s AND m.conversation_id=%s AND m.position<=(SELECT position FROM messages WHERE id=%s) LIMIT 1',(source['attachment_id'],candidate['source_conversation_id'],candidate['source_message_id'])).fetchone() if source else None
    if candidate['status'] in ('rejected','expired') or source is None or source['status']!='ready' or source['created_at']!=source['source_version'] or source['conversation_id']!=candidate['source_conversation_id'] or not sent or not (directory/'attachments'/str(source['attachment_id'])).is_file() or not source['is_image'] and candidate['source_quote'] not in source['content']:
        raise ValueError('附件来源已经失效或原文发生变化，不能提交旧候选。')
    if require_person and (candidate['subject']=='unknown' or not source['subject_label']):
        raise ValueError('请先明确这条事实属于你还是其他具体人物，再核对事实并确认保存。')


def clarify_subject(database: Database, directory: Path, identifier: UUID, kind: str, label: str) -> dict[str, Any]:
    """只允许未批准候选澄清人物，并与来源删除锁同一会话，避免晚到澄清重建已清正文。"""
    with database.connect() as connection:
        connection.execute('SELECT pg_advisory_xact_lock(%s)',(identifier.int % (2**63-1),))
        row=connection.execute('SELECT * FROM memory_candidates WHERE id=%s',(identifier,)).fetchone()
        if row is None or row['source_kind']!='attachment' or row['status']!='pending' or row['approved_at'] is not None:
            raise ValueError('只能为尚未批准的附件候选澄清人物。')
        if not connection.execute('SELECT id FROM conversations WHERE id=%s AND NOT deleting FOR SHARE',(row['source_conversation_id'],)).fetchone():
            raise ValueError('来源会话已删除，不能继续澄清。')
        validate_attachment_source(database,directory,row,False)
        source=connection.execute('SELECT * FROM attachment_memory_candidates WHERE candidate_id=%s',(identifier,)).fetchone()
        assert source is not None
        person='用户本人' if kind=='user' else label.strip()
        if not person or kind=='other' and person in ('我','自己','本人','你','用户本人'):
            raise ValueError('请填写其他人物的明确名称或关系。')
        connection.execute('UPDATE attachment_memory_candidates SET subject_label=%s WHERE candidate_id=%s',(person,identifier))
        connection.execute('UPDATE memory_candidates SET subject=%s,fact=%s,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s',(kind,f"人物：{person}。事实：{source['extracted_fact']}",identifier))
    return {'status':'clarified'}
