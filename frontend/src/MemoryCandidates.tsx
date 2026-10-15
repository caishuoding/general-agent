import { useState, type MouseEvent } from "react";
import { api, errorMessage, type MemoryCandidate, type Message } from "./api";

type Props = { message: Message; conversationId: string; onChanged: () => Promise<void> };

// 展示可核对的候选事实与来源；确认、拒绝及识别重试都重新读取持久化结果。
export function MemoryCandidates({ message, conversationId, onChanged }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // 对后端已绑定的候选执行操作，不从浏览器传入替换正文或目标。
  async function decide(event: MouseEvent<HTMLButtonElement>) {
    const { candidate, action } = event.currentTarget.dataset;
    if (!candidate || !action || busy) return;
    setBusy(true);
    setError("");
    try {
      await api(`/memory-candidates/${candidate}/${action}`, { method: "POST" });
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      try { await onChanged(); } catch (failure) { setError(errorMessage(failure)); }
      setBusy(false);
    }
  }

  // 识别失败时只重试这条原用户来源，重新产生的卡片仍需用户明确确认。
  async function retryDiscovery() {
    if (!message.reply_to || busy) return;
    setBusy(true);
    setError("");
    try {
      await api(`/conversations/${conversationId}/messages/${message.reply_to}/memory-candidates/retry`, { method: "POST" });
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      try { await onChanged(); } catch (failure) { setError(errorMessage(failure)); }
      setBusy(false);
    }
  }

  // 按真实业务状态呈现卡片；开始正式提交后只允许继续核对，不把批准伪装成未发生。
  function renderCandidate(candidate: MemoryCandidate) {
    const pending = candidate.status === "pending";
    const retryable = candidate.status === "failed" || candidate.status === "paused";
    const labels: Record<MemoryCandidate["status"], string> = {
      pending: `待确认 · ${candidate.target_id ? "变更长期记忆" : "新增长期记忆"}`,
      saving: "已确认，正在提交；尚未保存成功",
      paused: "已确认，提交等待预算许可",
      failed: "提交未完成，可以重试核对",
      confirmed: candidate.target_id ? "已确认并更新" : "已确认并保存",
      rejected: "已拒绝，未保存",
      expired: "候选已失效，未继续提交",
    };
    return <section className={`memory-candidate ${candidate.status}`} data-testid="memory-candidate" key={candidate.id}>
      <strong role="status">{labels[candidate.status]}</strong>
      <p className="candidate-fact">{candidate.fact}</p>
      <p>归属：你</p>
      {candidate.target_id && <p>目标条目：{candidate.target_fact}</p>}
      <p className="candidate-source">来源原文：{candidate.source_quote}</p>
      <a href={`#message-${candidate.source_message_id}`}>查看本会话原消息</a>
      {pending && <p className="candidate-note">确认后才成为长期记忆。有效至 {new Date(candidate.expires_at).toLocaleString("zh-CN")}。</p>}
      {candidate.error && <p role="status">{candidate.error}</p>}
      {(pending || retryable) && <div className="candidate-actions">
        <button className="retry-button" data-candidate={candidate.id} data-action="confirm" onClick={decide} disabled={busy}>{retryable ? "重试提交" : candidate.target_id ? "确认变更" : "确认保存"}</button>
        {pending && !candidate.approved_at && <button className="retry-button" data-candidate={candidate.id} data-action="reject" onClick={decide} disabled={busy}>拒绝</button>}
      </div>}
    </section>;
  }

  return <div className="memory-candidates">
    {message.memory_candidates?.map(renderCandidate)}
    {message.memory_discovery?.status === "failed" && <div className="memory-discovery" role="status">
      <p>候选识别未完成 · {message.memory_discovery.error}</p>
      <button className="retry-button" onClick={retryDiscovery} disabled={busy}>重试候选识别</button>
    </div>}
    {error && <p className="message-error" role="alert">{error}</p>}
  </div>;
}
