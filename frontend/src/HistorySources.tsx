import type { MouseEvent } from "react";
import type { Message } from "./api";

type Props = {
  state: Message["history_search"];
  sources: Message["history_sources"];
  onSource: (conversationId: string, messageId: string) => void;
};

// 展示服务端重新核验的旧消息摘录和出处，不提供跨会话附件读取入口。
export function HistorySources({ state, sources, onSource }: Props) {
  if (!state) return null;

  // 使用服务器提供的真实会话与消息标识打开原始讨论。
  function openSource(event: MouseEvent<HTMLButtonElement>) {
    const { conversation, message } = event.currentTarget.dataset;
    if (conversation && message) onSource(conversation, message);
  }

  // 将消息角色、日期及原文摘录作为可逐条核对的来源显示。
  function renderSource(source: Message["history_sources"][number]) {
    return <details className="file-citation" key={source.key} open>
      <summary>[{source.key}] {source.role === "user" ? "你" : "助手"} · {new Date(source.created_at).toLocaleString("zh-CN")}</summary>
      <button className="source-button" type="button" data-conversation={source.conversation_id} data-message={source.message_id} onClick={openSource}>{source.conversation_title} · 查看原消息</button>
      <blockquote>{source.excerpt}</blockquote>
    </details>;
  }

  return <section className="history-sources" aria-label="历史聊天出处">
    <div className="message-progress" role="status">{state.detail}</div>
    {sources.map(renderSource)}
  </section>;
}
