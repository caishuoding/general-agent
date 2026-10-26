import { useEffect, useState, type MouseEvent } from "react";
import { api, errorMessage, type AgentTask } from "./api";

type Source = { key: string; url?: string; title?: string; filename?: string; knowledge_base_id?: string; document_id?: string; page?: number };
type AgentRun = { run_id: string; sequence: number; task: string; status: string; result: string | null; error: string | null; created_at: string; started_at: string | null; finished_at: string | null; sources: Source[] };
type AgentThread = AgentTask & { runs: AgentRun[] };
const labels: Record<string, string> = { queued: "排队中", running: "执行中", completed: "已完成", failed: "失败", cancelled: "已取消", timed_out: "超时" };

// 返回稳定的中文状态标签，未知状态保留后端原文便于诊断。
function statusLabel(status: string) { return labels[status] || status; }

// 只为后端登记且权限有效的来源生成可打开地址，网页限制为 HTTP(S)。
function sourceHref(source: Source) {
  if (source.knowledge_base_id && source.document_id)
    return `/api/knowledge-bases/${encodeURIComponent(source.knowledge_base_id)}/documents/${encodeURIComponent(source.document_id)}/file${source.page ? `#page=${source.page}` : ""}`;
  return source.url && /^https?:\/\//i.test(source.url) ? source.url : null;
}

// 渲染一次执行的派工、公开回复、系统错误与真实来源，不展示内部模型消息。
function AgentRunItem({ run, title }: { run: AgentRun; title: string }) {
  // 为单轮来源创建稳定链接，无法安全打开的项目不渲染。
  function renderSource(source: Source) {
    const href = sourceHref(source);
    return href ? <li key={source.key}><a href={href} target="_blank" rel="noopener noreferrer">{source.filename || source.title || source.key}</a></li> : null;
  }
  return <section className="agent-run" aria-label={`第 ${run.sequence} 轮`}>
    <div className="agent-run-heading"><span>第 {run.sequence} 轮</span><span className={`agent-status agent-status-${run.status}`}>{statusLabel(run.status)}</span></div>
    <article className="agent-chat-message agent-chat-main">
      <strong>主 Agent</strong>
      <p>{run.task}</p>
    </article>
    {run.result && <article className="agent-chat-message agent-chat-subagent">
      <strong>{title}</strong>
      <p>{run.result}</p>
    </article>}
    {run.error && <p className="agent-chat-system" role="status">{run.error}</p>}
    {!!run.sources.length && <div className="agent-run-sources"><strong>来源</strong><ul aria-label={`第 ${run.sequence} 轮来源`}>{run.sources.map(renderSource)}</ul></div>}
  </section>;
}

// 读取并显示子智能体只读会话；执行状态变化时刷新，关闭后丢弃迟到响应。
function AgentConversation({ task, conversationId, answerId, onClose }: { task: AgentTask; conversationId: string; answerId: string; onClose: () => void }) {
  const [thread, setThread] = useState<AgentThread | null>(null);
  const [error, setError] = useState("");
  useEffect(
    // 按当前账号、回答和执行版本加载完整公开线程。
    function loadThread() {
      let active = true;
      setError("");
      api<AgentThread>(`/conversations/${conversationId}/messages/${answerId}/agents/${task.agent_id}/thread`).then(
        // 只接收当前打开的会话详情。
        function received(value) { if (active) setThread(value); },
        // 保留侧栏结构并显示可理解的授权或读取错误。
        function failed(value: unknown) { if (active) setError(errorMessage(value)); },
      );
      // 切换到其他子任务后禁止旧响应覆盖当前内容。
      return function discard() { active = false; };
    },
    [conversationId, answerId, task.agent_id, task.run_id, task.status],
  );
  useEffect(
    // 支持用 Escape 关闭只读会话，保持键盘操作与原生对话框一致。
    function listenForEscape() {
      // 仅在用户按下 Escape 时关闭当前会话。
      function closeOnEscape(event: KeyboardEvent) {
        if (event.key === "Escape") onClose();
      }
      window.addEventListener("keydown", closeOnEscape);
      // 卸载时移除全局键盘监听。
      return function cleanup() { window.removeEventListener("keydown", closeOnEscape); };
    },
    [onClose],
  );
  // 点击遮罩关闭侧栏，点击内容内部不关闭。
  function closeFromBackdrop(event: MouseEvent<HTMLDivElement>) { if (event.target === event.currentTarget) onClose(); }
  // 阻止会话内容上的点击冒泡到遮罩。
  function keepOpen(event: MouseEvent<HTMLElement>) { event.stopPropagation(); }
  const title = thread?.title || task.title;
  const status = thread?.status || task.status;
  return <div className="agent-conversation-backdrop" onMouseDown={closeFromBackdrop}>
    <aside className="agent-conversation" role="dialog" aria-modal="true" aria-label={title} onMouseDown={keepOpen}>
      <header className="agent-conversation-header">
        <div><p className="agent-conversation-kicker">子智能体会话</p><h2>{title}</h2></div>
        <div><span className={`agent-status agent-status-${status}`}>{statusLabel(status)}</span><button type="button" className="agent-conversation-close" aria-label="关闭子任务会话" onClick={onClose}>关闭</button></div>
      </header>
      <div className="agent-conversation-body">
        {error && <p role="alert">{error}</p>}
        {!thread && !error && <p role="status">正在读取会话…</p>}
        {thread?.runs.map(
          // 按后端 sequence 展示每轮派工与结果，主 Agent 追加任务自然进入同一条会话。
          function renderRun(run) { return <AgentRunItem key={run.run_id} run={run} title={title} />; },
        )}
      </div>
      <footer className="agent-conversation-footer">此会话由主智能体管理，无法直接输入。</footer>
    </aside>
  </div>;
}

// 显示回答下的轻量子任务入口，打开后提供只读会话而不混入主回答正文。
export function AgentTasks({ tasks, conversationId, answerId }: { tasks: AgentTask[]; conversationId: string; answerId: string }) {
  const [selected, setSelected] = useState<AgentTask | null>(null);
  if (!tasks.length) return null;
  let active: AgentTask | null = null;
  if (selected) {
    for (const task of tasks) {
      if (task.agent_id === selected.agent_id) active = task;
    }
  }
  // 用最新任务快照打开会话，主从执行轮次变化后详情会自动刷新。
  function openTask(task: AgentTask) { setSelected(task); }
  // 清空当前只读会话选择。
  function closeTask() { setSelected(null); }
  return <section className="agent-tasks" aria-label="子任务">
    <div className="agent-tasks-title">子任务 · {tasks.length}</div>
    {tasks.map(
      // 为每个子 Agent 保留稳定入口和当前执行轮次。
      function renderTask(task) {
        // 打开当前任务对应的只读子会话。
        function activateTask() { openTask(task); }
        return <div className="agent-task-card" key={task.agent_id}>
          <button type="button" className="agent-task-button" onClick={activateTask} aria-label={`${task.title} · ${statusLabel(task.status)}`}><span>{task.title}</span><span className={`agent-status agent-status-${task.status}`}>{statusLabel(task.status)}</span></button>
          {task.result && <p className="agent-task-result">{task.result}</p>}
        </div>;
      },
    )}
    {active && <AgentConversation task={active} conversationId={conversationId} answerId={answerId} onClose={closeTask} />}
  </section>;
}
