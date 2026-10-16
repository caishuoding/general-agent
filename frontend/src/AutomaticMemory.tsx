import { useState } from "react";
import { api, errorMessage, type Message } from "./api";

// 展示自动处理的真实结果，失败时独立重试原来源，无需再次批准具体事实。
export function AutomaticMemory({ message, conversationId, onChanged }: { message: Message; conversationId: string; onChanged: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // 重试只传来源标识，预算暂停由共用请求流程处理。
  async function retry() {
    if (!message.reply_to || busy) return;
    setBusy(true);
    setError("");
    try {
      await api(`/conversations/${conversationId}/messages/${message.reply_to}/memory/retry`, { method: "POST" });
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      try { await onChanged(); } catch (failure) { setError(errorMessage(failure)); }
      setBusy(false);
    }
  }

  if (!message.automatic_memory) return null;
  return <div data-testid="automatic-memory" className={`memory-action ${message.automatic_memory.status}`} role="status">
    <p>{message.automatic_memory.detail}</p>
    {message.automatic_memory.status === "failed" && message.status === "complete" && <button className="retry-button" onClick={retry} disabled={busy}>{busy ? "正在重试…" : "重试自动记忆"}</button>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
