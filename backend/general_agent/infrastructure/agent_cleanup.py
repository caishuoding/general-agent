"""在资料撤销事务中清除主从执行、结果信箱的派生正文与外发许可。"""

from typing import Any
from uuid import UUID


def forget_checkpoint_material(connection: Any, conversation_ids: list[UUID], answer_ids: list[UUID] | None = None) -> None:
    """按撤销范围清除派生 State，历史单回答清理不能影响当前新执行。"""
    exists = connection.execute("SELECT to_regclass('langgraph.checkpoints') AS name").fetchone()
    if not exists or not exists['name']:
        return
    for identifier in sorted(conversation_ids):
        connection.execute('SELECT pg_advisory_xact_lock(%s)', (identifier.int % (2**63 - 1),))
    scope = 'conversation_id=ANY(%s)' + (' AND answer_id=ANY(%s)' if answer_ids is not None else '')
    arguments = (conversation_ids, answer_ids) if answer_ids is not None else (conversation_ids,)
    for table in ('checkpoint_writes', 'checkpoint_blobs', 'checkpoints'):
        connection.execute(f'DELETE FROM langgraph.{table} WHERE thread_id IN (SELECT thread_id FROM checkpoint_threads WHERE {scope})', arguments)
    connection.execute(f'DELETE FROM checkpoint_threads WHERE {scope}', arguments)



def invalidate_agent_material(connection: Any, conversation_ids: list[UUID], answer_ids: list[UUID] | None = None) -> None:
    """撤销派生检查点、回答正文和信箱交付，旧资料不能通过摘要续接。"""
    if not conversation_ids:
        return
    forget_checkpoint_material(connection, conversation_ids, answer_ids)
    if answer_ids is None:
        answer_ids = [row['answer_attempt_id'] for row in connection.execute('SELECT DISTINCT answer_attempt_id FROM agents WHERE conversation_id=ANY(%s)', (conversation_ids,)).fetchall()]
    if not answer_ids:
        return
    connection.execute('SELECT id FROM agents WHERE answer_attempt_id=ANY(%s) AND parent_agent_id IS NULL ORDER BY id FOR UPDATE', (answer_ids,))
    connection.execute('UPDATE agents SET closed=TRUE,title=%s WHERE answer_attempt_id=ANY(%s)', ('资料已撤销', answer_ids))
    connection.execute("UPDATE messages SET status='stopped',content='',error='资料已撤销，请重新提问。' WHERE status IN ('generating','interrupted') AND id IN (SELECT answer_attempt_id FROM agents WHERE answer_attempt_id=ANY(%s))", (answer_ids,))
    connection.execute('UPDATE cloud_operations SET cancelled=TRUE,invalidated=TRUE,result=NULL,response_status=NULL WHERE id IN (SELECT r.operation_id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=ANY(%s))', (answer_ids,))
    connection.execute("UPDATE search_requests SET status='expired',query='',completed_queries='{}',results='[]',sources='[]' WHERE operation_id IN (SELECT r.operation_id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=ANY(%s))", (answer_ids,))
    connection.execute("UPDATE agent_runs SET status=CASE WHEN status IN ('queued','running') THEN 'cancelled' ELSE status END,task='',result=NULL,messages='[]',error='source_invalidated',finished_at=coalesce(finished_at,CURRENT_TIMESTAMP) WHERE agent_id IN (SELECT id FROM agents WHERE answer_attempt_id=ANY(%s))", (answer_ids,))
    for table in ('agent_messages', 'agent_sources'):
        connection.execute(f'DELETE FROM {table} WHERE run_id IN (SELECT r.id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=ANY(%s))', (answer_ids,))
    connection.execute('DELETE FROM agent_tool_requests WHERE parent_run_id IN (SELECT r.id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=ANY(%s))', (answer_ids,))

    connection.execute('DELETE FROM agent_inbox_events WHERE recipient_run_id IN (SELECT r.id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=ANY(%s))', (answer_ids,))
    connection.execute('DELETE FROM agent_inbox_deliveries WHERE recipient_run_id IN (SELECT r.id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=ANY(%s))', (answer_ids,))
