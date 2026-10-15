import { useEffect, useState, type MouseEvent } from "react";
import { api, errorMessage, type MemoryAction } from "./api";

type Props = { onSource: (conversationId: string, messageId: string) => void };
type Entry = MemoryAction & { conversation_title: string };

// 读取正式记忆的事实、来源和真实保存状态，失败不显示成已保存。
export function MemoryPanel({ onSource }: Props) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(
    // 打开管理页或主动刷新时重新核对正式存储，卸载后忽略迟到结果。
    () => {
      let active = true;
      // 完成当前页读取，分别呈现读取失败与空列表。
      async function load() {
        setLoading(true); setError("");
        try { const result = await api<Entry[]>("/memories"); if (active) setEntries(result); }
        catch (failure) { if (active) setError(errorMessage(failure)); }
        finally { if (active) setLoading(false); }
      }
      void load();
      // 停止向已经离开的管理页写状态。
      return () => { active = false; };
    }, [revision],
  );
  // 用户明确刷新时触发重新读取，不自动重试记忆写入。
  function reload() { setRevision(revision + 1); }
  // 根据后端保存的会话及消息标识返回原始来源。
  function openSource(event: MouseEvent<HTMLButtonElement>) {
    const { conversation, message } = event.currentTarget.dataset;
    if (conversation && message) onSource(conversation, message);
  }
  // 每个条目分别展示真实状态，来源删除与编辑能力由相应后续流程补充。
  function renderEntry(entry: Entry) {
    return <article className="memory-entry" data-testid="memory-entry" key={entry.id}>
      <div className={`memory-status ${entry.status}`}>{entry.status === "complete" ? "已保存" : entry.status === "failed" ? "保存未完成" : "等待保存"}</div>
      <p className="memory-fact">{entry.fact}</p>
      {entry.error && <p className="message-error" role="status">{entry.error}</p>}
      <div className="memory-source"><span>来源：{entry.conversation_title}</span><button onClick={openSource} data-conversation={entry.conversation_id} data-message={entry.message_id}>查看原消息</button></div>
      <time>{new Date(entry.created_at).toLocaleString("zh-CN")}</time>
    </article>;
  }
  return <section className="memory-management page-scroll">
    <div className="eyebrow">关于你的长期记忆</div><h2>已批准的长期记忆</h2>
    <p className="memory-intro">在聊天中明确说“请记住：具体事实”即可保存。普通聊天内容不会直接写入。</p>
    <button className="retry-button" onClick={reload} disabled={loading}>刷新记忆</button>
    {loading && <p role="status">正在读取记忆…</p>}
    {error && <p role="alert">{error}</p>}
    {!loading && !error && !entries.length && <p className="memory-empty">还没有保存的记忆。你可以试试：“请记住：我喜欢简洁的中文回答”。</p>}
    <div className="memory-list">{entries.map(renderEntry)}</div>
  </section>;
}
