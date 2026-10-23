import { useEffect, useState, type SyntheticEvent } from "react";
import { api, errorMessage, type AgentTask } from "./api";

type Source = { key: string; url?: string; title?: string; filename?: string; knowledge_base_id?: string; document_id?: string; page?: number };
type Detail = AgentTask & { task: string; result: string | null; sources: Source[] };
const labels: Record<string, string> = { queued: "排队中", running: "执行中", completed: "已完成", failed: "失败", cancelled: "已取消", timed_out: "超时" };

// 按需加载一个子任务的公开交付，不下载模型工作历史或推理内容。
function AgentTaskItem({ task, conversationId, answerId }: { task: AgentTask; conversationId: string; answerId: string }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  useEffect(
    // 展开或执行状态变化后刷新详情，丢弃旧执行和旧会话的迟到响应。
    function loadTaskDetail() {
      if (!open) return;
      let active = true;
      setDetail(null);
      setError("");
      api<Detail>(`/conversations/${conversationId}/messages/${answerId}/agents/${task.agent_id}`).then(
        // 仅接收当前展开项的最新详情。
        function received(value) { if (active) setDetail(value); },
        // 读取失败保留任务列表，并展示可理解的错误。
        function failed(value: unknown) { if (active) setError(errorMessage(value)); },
      );
      // 会话切换或状态变化后禁止旧响应覆盖新内容。
      return function discard() { active = false; };
    },
    [open, conversationId, answerId, task.agent_id, task.run_id, task.status],
  );
  // 将原生详情展开状态同步到按需加载逻辑。
  function toggle(event: SyntheticEvent<HTMLDetailsElement>) { setOpen(event.currentTarget.open); }
  // 只为后端已登记来源生成原文链接，网页限制为 HTTP(S)。
  function renderSource(source: Source) {
    const href = source.knowledge_base_id && source.document_id
      ? `/api/knowledge-bases/${encodeURIComponent(source.knowledge_base_id)}/documents/${encodeURIComponent(source.document_id)}/file${source.page ? `#page=${source.page}` : ""}`
      : source.url && /^https?:\/\//i.test(source.url) ? source.url : null;
    return href ? <li key={source.key}><a href={href} target="_blank" rel="noopener noreferrer">{source.filename || source.title || source.key}</a></li> : null;
  }
  return <details onToggle={toggle} className="agent-task">
    <summary>{task.title} · {labels[task.status] || task.status}</summary>
    {open && <div>
      {error && <p role="alert">{error}</p>}
      {!detail && !error && <p role="status">正在读取任务…</p>}
      {detail && <><p>{detail.task}</p>{detail.result && <pre className="agent-result">{detail.result}</pre>}{detail.error && <p role="status">{detail.error}</p>}{!!detail.sources?.length && <ul aria-label="子任务来源">{detail.sources.map(renderSource)}</ul>}</>}
    </div>}
  </details>;
}

// 显示回答下的轻量子任务快照，主回答正文仍由主智能体统一输出。
export function AgentTasks({ tasks, conversationId, answerId }: { tasks: AgentTask[]; conversationId: string; answerId: string }) {
  if (!tasks.length) return null;
  // 为各子任务保留独立展开及加载状态。
  function renderTask(task: AgentTask) { return <AgentTaskItem key={task.agent_id} task={task} conversationId={conversationId} answerId={answerId} />; }
  return <section className="agent-tasks" aria-label="子任务"><div>子任务 · {tasks.length}</div>{tasks.map(renderTask)}</section>;
}
