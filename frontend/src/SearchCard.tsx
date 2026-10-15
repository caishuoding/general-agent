import { useEffect, useState, type ChangeEvent, type MouseEvent } from 'react';
import { api, errorMessage, type ConversationDetail, type Message } from './api';

type Props = { message: Message; conversationId: string; onUpdated: (detail: ConversationDetail) => void };

// 展示持久化外发查询与用途，编辑、拒绝和批准各自对应明确的后端动作。
export function SearchCard({ message, conversationId, onUpdated }: Props) {
  const action = message.search_action;
  const [query, setQuery] = useState(action?.query || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(
    // 查询修订改变后展示服务器保存的完整文本，不沿用旧批准。
    () => { setQuery(action?.query || ''); }, [action?.revision, action?.query],
  );
  // 仅编辑本地文本；内容改变后批准按钮禁用，必须先保存为新修订。
  function edit(event: ChangeEvent<HTMLTextAreaElement>) { setQuery(event.currentTarget.value); }
  // 保存完整查询，后端换发确认修订但不会发送任何搜索请求。
  async function save() {
    if (!action) return;
    setBusy(true); setError('');
    try { onUpdated(await api<ConversationDetail>(`/conversations/${conversationId}/search/${action.id}`, { method: 'PUT', body: JSON.stringify({ revision: action.revision, query }) })); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  // 批准或拒绝当前显示修订，响应刷新真实消息状态以重新订阅执行进度。
  async function decide(event: MouseEvent<HTMLButtonElement>) {
    if (!action) return;
    const decision = event.currentTarget.dataset.decision;
    setBusy(true); setError('');
    try { onUpdated(await api<ConversationDetail>(`/conversations/${conversationId}/search/${action.id}/decision`, { method: 'POST', body: JSON.stringify({ revision: action.revision, decision }) })); }
    catch (failure) {
      setError(errorMessage(failure));
      try { onUpdated(await api<ConversationDetail>(`/conversations/${conversationId}`)); }
      catch { /* 刷新失败时保留原错误，下一次用户操作仍由服务端校验权限。 */ }
    }
    finally { setBusy(false); }
  }
  // 在已批准但进程尚未恢复时继续原执行，不产生新的外发许可。
  async function resume() {
    setBusy(true); setError('');
    try { onUpdated(await api<ConversationDetail>(`/conversations/${conversationId}/messages/${message.id}/resume`, { method: 'POST' })); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  if (!action) return null;
  if (action.status !== 'pending') return <div className={`search-status ${action.status}`} role="status">
    {({ planning: '正在准备搜索内容，尚未联网', approved: '搜索已获许可，等待执行', complete: '联网搜索已完成', rejected: '已拒绝外发，本次未联网', expired: '外发许可已失效', cancelled: '搜索已取消', failed: '联网搜索未完成' } as Record<string, string>)[action.status] || action.status}
    {action.error && <p>{action.error}</p>}
    {action.status === 'approved' && message.status === 'paused' && !message.budget_pause && <button className="retry-button" onClick={resume} disabled={busy}>继续本次搜索</button>}
    {error && <p role="alert">{error}</p>}
  </div>;
  return <section className="search-confirm" aria-label="搜索内容外发确认">
    <strong>发送这些内容到搜索服务？</strong>
    <p>{action.purpose}</p>
    <p>查询已由你配置的模型根据相关资料生成。请核对并删去不需要发送的细节。</p>
    <p>来源：{action.sources.map(
      // 展示参与查询的来源名称，正文以完整查询框中的文本为准。
      (source) => source.label,
    ).join('、')}</p>
    <label>将发送给 Tavily 的完整查询<textarea value={query} onChange={edit} maxLength={2000} disabled={busy} /></label>
    <p>批准仅用于当前查询的一次搜索，有效期 30 分钟；修改文本或来源变化后需要重新确认。预算许可另外检查。</p>
    <div className="search-confirm-actions">
      <button className="secondary-button" onClick={save} disabled={busy || !query.trim() || query === action.query}>保存查询修改</button>
      <button className="secondary-button" data-decision="reject" onClick={decide} disabled={busy}>拒绝发送</button>
      <button className="primary-button" data-decision="approve" onClick={decide} disabled={busy || query !== action.query}>允许发送以上内容并搜索</button>
    </div>
    {error && <p role="alert">{error}</p>}
  </section>;
}
