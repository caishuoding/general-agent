import { useEffect, useState, type ChangeEvent, type FormEvent, type MouseEvent } from "react";
import { api, errorMessage, type AttachmentMemorySource, type MemoryAction, type MemoryUpdate } from "./api";

type Props = { onSource: (conversationId: string, messageId: string) => void };
type Entry = MemoryAction & { conversation_title: string; version: number; updates: MemoryUpdate[]; attachment_sources: AttachmentMemorySource[] };

// 读取真实记忆和清理状态，支持编辑、忘记、恢复失败操作并查看原聊天来源。
export function MemoryPanel({ onSource }: Props) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  const [editing, setEditing] = useState<Entry | null>(null);
  const [fact, setFact] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(
    // 打开管理页或主动刷新时重新核对正式存储，卸载后忽略迟到结果。
    () => {
      let active = true;
      // 完成当前页读取，分别呈现读取失败与空列表。
      async function load() {
        setLoading(true);
        try { const result = await api<Entry[]>("/memories"); if (active) setEntries(result); }
        catch (failure) { if (active) setError(errorMessage(failure)); }
        finally { if (active) setLoading(false); }
      }
      void load();
      // 停止向已经离开的管理页写状态。
      return () => { active = false; };
    }, [revision],
  );
  // 用户明确刷新时重新核验存储，不自动重试写入。
  function reload() { setRevision(revision + 1); }
  // 根据后端保存的会话及消息标识返回原始或更正来源。
  function openSource(event: MouseEvent<HTMLButtonElement>) {
    const { conversation, message } = event.currentTarget.dataset;
    if (conversation && message) onSource(conversation, message);
  }
  // 将选中条目的当前版本带入编辑，防止旧页面覆盖其他更正。
  function openEdit(event: MouseEvent<HTMLButtonElement>) {
    for (const entry of entries) if (entry.id === event.currentTarget.dataset.entry) {
      setEditing(entry); setFact(entry.fact); setError("");
    }
  }
  // 保存用户正在编辑的正文，只有点击提交才形成更正授权。
  function changeFact(event: ChangeEvent<HTMLTextAreaElement>) { setFact(event.target.value); }
  // 放弃尚未提交的草稿，不撤销已经提交的记忆操作。
  function cancelEdit() { setEditing(null); setFact(""); }
  // 使用明确条目和版本提交，统一预算弹窗恢复时复用相同操作标识。
  async function saveEdit(event: FormEvent) {
    event.preventDefault();
    if (!editing || !fact.trim()) return;
    setBusy(editing.id); setError("");
    try {
      await api(`/memories/${editing.id}`, { method: "PUT", headers: { "X-Operation-ID": crypto.randomUUID() }, body: JSON.stringify({ fact, expected_version: editing.version }) });
      setEditing(null);
    } catch (failure) { setError(errorMessage(failure)); setEditing(null); }
    finally { setBusy(null); reload(); }
  }
  // 显式恢复已保存的更正目标；不再次递增版本或猜测其他条目。
  async function retryUpdate(event: MouseEvent<HTMLButtonElement>) {
    const { update, entry } = event.currentTarget.dataset;
    if (!update || !entry) return;
    setBusy(entry); setError("");
    try { await api(`/memory-updates/${update}/retry`, { method: "POST" }); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(null); reload(); }
  }
  // 明确点击只删除选中长期记忆；原聊天保留，失败显示停用状态并可重试本地清理。
  async function forgetEntry(event: MouseEvent<HTMLButtonElement>) {
    const identifier = event.currentTarget.dataset.entry;
    if (!identifier) return;
    setBusy(identifier); setError(""); setNotice(""); setEditing(null);
    try {
      const result = await api<{ detail: string }>(`/memories/${identifier}`, { method: "DELETE" });
      setNotice(result.detail);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(null); reload(); }
  }
  // 展示每次更正的批准来源，管理页操作与聊天消息分别标记。
  function renderUpdate(update: MemoryUpdate) {
    return <div className="memory-update-source" key={update.id}>
      <span>{update.source_kind === "management" ? "管理页更正" : `聊天更正：${update.conversation_title || "原会话"}`} · {update.status === "complete" ? "已完成" : "未完成"}</span>
      {update.source_message_id && update.source_conversation_id && <button onClick={openSource} data-conversation={update.source_conversation_id} data-message={update.source_message_id}>查看更正消息</button>}
    </div>;
  }
  // 删除中的条目不再展示事实，显式提供清理重试；其余条目按真实更正状态展示。
  function renderEntry(entry: Entry) {
    // 文件下载同时核验原会话，人物沿用用户批准的精确预览。
    function renderAttachmentSource(source: AttachmentMemorySource) {
      return <p key={source.attachment_id}>附件来源：<a href={`/api/conversations/${entry.conversation_id}/attachments/${source.attachment_id}/file`}>{source.filename}</a> · {source.location} · {source.subject_label}</p>;
    }
    const update = entry.updates.at(-1);
    const changing = busy === entry.id;
    const unfinished = update && update.status !== "complete" && update.status !== "deleted";
    return <article className="memory-entry" data-testid="memory-entry" key={entry.id}>
      <div className={`memory-status ${changing ? "pending" : entry.status}`}>{changing ? "正在处理，尚未确认完成" : entry.status === "deleting" ? "已停止召回 · 清理未完成" : entry.status === "complete" ? "已保存" : unfinished ? "更正未完成 · 旧事实已停止召回" : entry.status === "failed" ? "保存未完成" : "等待保存"}</div>
      <p className="memory-fact">{unfinished ? update.fact : entry.fact}</p>
      {entry.deletion?.error && <p className="message-error" role="status">{entry.deletion.error}</p>}
      {entry.error && <p className="message-error" role="status">{entry.error}</p>}
      {editing?.id === entry.id ? <form className="memory-edit" onSubmit={saveEdit}>
        <label>新的记忆事实<textarea value={fact} onChange={changeFact} maxLength={5000} disabled={changing} required /></label>
        <div><button className="retry-button" type="submit" disabled={changing || !fact.trim()}>保存更正</button><button type="button" onClick={cancelEdit} disabled={changing}>取消编辑</button></div>
      </form> : <div className="memory-edit-actions">
        <button className="retry-button" onClick={forgetEntry} data-entry={entry.id} disabled={!!busy}>{entry.status === "deleting" ? "重试清理记忆" : "忘记这条记忆"}</button>
        {entry.status === "complete" && <button className="retry-button" onClick={openEdit} data-entry={entry.id} disabled={!!busy}>编辑记忆</button>}
        {unfinished && update.status !== "saving" && <button className="retry-button" onClick={retryUpdate} data-update={update.id} data-entry={entry.id} disabled={!!busy}>重试更正</button>}
      </div>}
      <div className="memory-source"><span>原来源：{entry.conversation_title}</span><button onClick={openSource} data-conversation={entry.conversation_id} data-message={entry.message_id}>查看原消息</button></div>
      {entry.attachment_sources.map(renderAttachmentSource)}
      {entry.updates.map(renderUpdate)}
      <div className="memory-identity">记忆标识：{entry.id}</div>
      <time>{new Date(entry.created_at).toLocaleString("zh-CN")}</time>
    </article>;
  }
  return <section className="memory-management page-scroll">
    <div className="eyebrow">关于你的长期记忆</div><h2>已批准的长期记忆</h2>
    <p className="memory-intro">明确说“请记住：具体事实”即可保存。忘记会清除长期记忆及派生副本，原聊天仍保留。可在这里编辑，或发送“更正记忆：旧事实 =&gt; 新事实”；目标不明确时会先澄清。</p>
    <button className="retry-button" onClick={reload} disabled={loading || !!busy}>刷新记忆</button>
    {loading && <p role="status">正在读取记忆…</p>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {!loading && !error && !entries.length && <p className="memory-empty">还没有保存的记忆。你可以试试：“请记住：我喜欢简洁的中文回答”。</p>}
    <div className="memory-list">{entries.map(renderEntry)}</div>
  </section>;
}
