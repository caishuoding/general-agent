"""在资料撤销事务中清除主从执行的派生正文与外发许可。"""

from typing import Any
from uuid import UUID


def invalidate_agent_material(connection: Any, conversation_ids: list[UUID], answer_ids: list[UUID] | None = None) -> None:
    """撤销指定回答的派生正文；真实资料变更可撤销会话全部任务，历史读取仅清理原回答。"""
    if not conversation_ids:
        return
    if answer_ids is None:
        answer_ids = [row['answer_attempt_id'] for row in connection.execute('SELECT DISTINCT answer_attempt_id FROM agents WHERE conversation_id=ANY(%s)', (conversation_ids,)).fetchall()]
    if not answer_ids:
        return
    connection.execute('SELECT id FROM agents WHERE answer_attempt_id=ANY(%s) AND parent_agent_id IS NULL ORDER BY id FOR UPDATE', (answer_ids,))
    connection.execute('UPDATE agents SET closed=TRUE,title=%s WHERE answer_attempt_id=ANY(%s)', ('资料已撤销', answer_ids))
    connection.execute("UPDATE messages SET status='stopped',content='',error='资料已撤销，请重新提问。' WHERE status='generating' AND id IN (SELECT answer_attempt_id FROM agents WHERE answer_attempt_id=ANY(%s))", (answer_ids,))
    connection.execute('UPDATE cloud_operations SET cancelled=TRUE,invalidated=TRUE,result=NULL,response_status=NULL WHERE id IN (SELECT r.operation_id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=ANY(%s))', (answer_ids,))
    connection.execute("UPDATE search_requests SET status='expired',query='',completed_queries='{}',results='[]',sources='[]' WHERE operation_id IN (SELECT r.operation_id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=ANY(%s))", (answer_ids,))
    connection.execute("UPDATE agent_runs SET status=CASE WHEN status IN ('queued','running') THEN 'cancelled' ELSE status END,task='',result=NULL,messages='[]',error='source_invalidated',finished_at=coalesce(finished_at,CURRENT_TIMESTAMP) WHERE agent_id IN (SELECT id FROM agents WHERE answer_attempt_id=ANY(%s))", (answer_ids,))
    for table in ('agent_messages', 'agent_sources'):
        connection.execute(f'DELETE FROM {table} WHERE run_id IN (SELECT r.id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=ANY(%s))', (answer_ids,))
    connection.execute('DELETE FROM agent_tool_requests WHERE parent_run_id IN (SELECT r.id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=ANY(%s))', (answer_ids,))
