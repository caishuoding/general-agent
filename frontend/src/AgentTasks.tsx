import { useEffect, useRef, useState, type MouseEvent } from "react";
import { AnswerMarkdown, sourceId } from "./AnswerContent";
import { api, errorMessage, type AgentTask } from "./api";

type Source = { key: string; url?: string; title?: string; excerpt?: string; filename?: string; site_name?: string; knowledge_base_id?: string; document_id?: string; page?: number };
type AgentRun = { run_id: string; sequence: number; task: string; status: string; result: string | null; error: string | null; created_at: string; started_at: string | null; finished_at: string | null; delivered: boolean; sources: Source[] };
type AgentThread = AgentTask & { runs: AgentRun[] };
const labels: Record<string, string> = { queued: "排队中", running: "执行中", completed: "已完成", failed: "失败", cancelled: "已取消", timed_out: "超时" };

// 返回稳定的中文状态标签，未知状态保留后端原文便于诊断。
function statusLabel(status: string) { return labels[status] || status; }

/**
 * 从子智能体标题提取一至两个首字，生成紧凑且可区分的圆形头像内容。
 *
 * @param title 子智能体显示标题。
 * @returns 中文字符优先取前两字，英文标题取词首，缺少有效字符时返回 `AI`。
 */
function agentInitials(title: string) {
  const characters = Array.from(title.trim());
  const hanCharacters = characters.filter(function isHanCharacter(character) {
    // 中文字符优先取前两个字，便于“深圳核查”“上海核查”等任务快速识别。
    const codePoint = character.codePointAt(0) || 0;
    return codePoint >= 0x3400 && codePoint <= 0x9fff;
  });
  if (hanCharacters.length) return hanCharacters.slice(0, 2).join("");
  const words = title.trim().split(/\s+/).filter(Boolean);
  // 英文标题使用前两个单词首字母；只有一个单词时保留两个字符以维持头像平衡。
  if (words.length >= 2) return words.slice(0, 2).map(function firstLetter(word) { return word[0]; }).join("").toUpperCase();
  return words[0]?.slice(0, 2).toUpperCase() || "AI";
}

/**
 * 根据稳定 agent ID 选择头像配色。
 *
 * @param agentId 子智能体持久化身份。
 * @returns 零到五之间的稳定色板序号，使同一智能体刷新后保持相同外观。
 */
function agentTone(agentId: string) {
  let hash = 0;
  for (const character of agentId) hash = (hash * 31 + (character.codePointAt(0) || 0)) >>> 0;
  return hash % 6;
}

// 只为后端登记且权限有效的来源生成可打开地址，网页限制为 HTTP(S)。
function sourceHref(source: Source) {
  if (source.knowledge_base_id && source.document_id)
    return `/api/knowledge-bases/${encodeURIComponent(source.knowledge_base_id)}/documents/${encodeURIComponent(source.document_id)}/file${source.page ? `#page=${source.page}` : ""}`;
  return source.url && /^https?:\/\//i.test(source.url) ? source.url : null;
}


// 生成来源的规范身份，合并 Markdown 版本、末尾斜杠及常见追踪参数。
function sourceIdentity(source: Source) {
  if (source.knowledge_base_id && source.document_id)
    return `kb:${source.knowledge_base_id}:${source.document_id}:${source.page || ""}`;
  if (source.url) {
    try {
      const parsed = new URL(source.url);
      parsed.hash = "";
      for (const key of [...parsed.searchParams.keys()]) {
        if (key.startsWith("utm_") || ["gspk", "gsxid", "irclickid", "wid"].includes(key)) parsed.searchParams.delete(key);
      }
      const path = parsed.pathname.replace(/\.md$/i, "").replace(/\/+$/, "");
      return `${parsed.hostname.toLowerCase()}${path}${parsed.search ? "?" + parsed.searchParams.toString() : ""}`;
    } catch {
      return source.url;
    }
  }
  return source.title || source.filename || source.key;
}

// 按规范 URL 去重来源，只保留第一条可展示记录并忽略辅助字段差异。
function uniqueSources(sources: Source[]) {
  const seen = new Set<string>();
  const unique: Source[] = [];
  for (const source of sources) {
    const identity = sourceIdentity(source);
    if (seen.has(identity)) continue;
    seen.add(identity);
    unique.push(source);
  }
  return unique;
}

// 为缺少标题的来源生成可读名称，避免把内部 W 编号直接展示给用户。
function sourceLabel(source: Source) {
  const explicit = source.filename || source.title?.trim();
  if (explicit) return explicit;
  if (source.site_name) return source.site_name;
  if (source.url) {
    try {
      const parsed = new URL(source.url);
      const filename = decodeURIComponent(parsed.pathname.split("/").filter(Boolean).at(-1) || "");
      return filename ? `${parsed.hostname} · ${filename}` : parsed.hostname;
    } catch {
      return source.url;
    }
  }
  return "未命名来源";
}

// 为单轮子消息的来源生成隔离锚点，避免同一回答内重复编号相互冲突。
function agentSourceId(runId: string, key: string) {
  return sourceId(`agent-run-${runId}`, key);
}

// 将公开交付正文转换成统一 Markdown 渲染器可识别的消息形状。
function agentMessage(run: AgentRun, sources: Source[]) {
  return {
    id: `agent-run-${run.run_id}`,
    content: run.result || "",
    web_sources: sources.map(function webSource(source) {
      // 只保留统一 Markdown 引用识别所需的公开字段。
      return { key: source.key, title: sourceLabel(source), url: source.url || "", excerpt: source.excerpt || "" };
    }),
    knowledge_sources: [],
  };
}

// 渲染一次执行的派工、公开回复、系统错误与真实来源，不展示内部模型消息。
function AgentRunItem({ run, title }: { run: AgentRun; title: string }) {
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const pendingSource = useRef<string | null>(null);
  const sources = uniqueSources(run.sources);
  const message = agentMessage(run, sources);
  // 来源区首次展开后读取待定位锚点。
  useEffect(function revealPendingAgentSource() {
    // 来源区完成渲染后再定位，确保首次点击子消息引用可见。
    if (!sourcesOpen || !pendingSource.current) return;
    const target = document.getElementById(pendingSource.current);
    pendingSource.current = null;
    // 等浏览器完成列表布局后再滚动和聚焦。
    if (target) requestAnimationFrame(function focusAgentSource() {
      target.scrollIntoView({ block: 'center' });
      target.focus({ preventScroll: true });
    });
  }, [sourcesOpen]);
  // 展开当前子消息的来源区并定位引用；已展开时直接滚动。
  function revealSource(identifier: string) {
    if (sourcesOpen) {
      const target = document.getElementById(identifier);
      // 已展开时无需重渲染，下一帧直接滚动。
      if (target) requestAnimationFrame(function scrollOpenAgentSource() {
        target.scrollIntoView({ block: 'center' });
        target.focus({ preventScroll: true });
      });
      return;
    }
    pendingSource.current = identifier;
    setSourcesOpen(true);
  }
  // 切换当前子消息来源的展开状态，默认保持隐藏。
  function toggleSources() {
    /** 反转当前来源区的展开状态。 */
    function invert(open: boolean) { return !open; }
    setSourcesOpen(invert);
  }
  // 为单轮来源创建稳定链接并使用可读名称，无法安全打开的项目不渲染。
  function renderSource(source: Source) {
    const href = sourceHref(source);
    return href ? <li key={source.key} id={agentSourceId(run.run_id, source.key)} tabIndex={-1}><a href={href} target="_blank" rel="noopener noreferrer">{sourceLabel(source)}</a></li> : null;
  }
  return <section className="agent-run" aria-label={`第 ${run.sequence} 轮`}>
    <div className="agent-run-heading"><span>第 {run.sequence} 轮</span><span className={`agent-status agent-status-${run.status}`}>{statusLabel(run.status)}</span></div>
    <article className="agent-chat-message agent-chat-main">
      <strong>主 Agent</strong>
      <p>{run.task}</p>
    </article>
    {run.delivered && run.result && <article className="agent-chat-message agent-chat-subagent">
      <strong>{title}</strong>
      <AnswerMarkdown message={message} onCitationClick={revealSource} className="agent-message-markdown" />
      {!!sources.length && <div className="agent-message-sources">
        <button type="button" aria-expanded={sourcesOpen} onClick={toggleSources}>参考来源 · {sources.length}</button>
        {sourcesOpen && <ul aria-label={`第 ${run.sequence} 轮来源`}>{sources.map(renderSource)}</ul>}
      </div>}
    </article>}
    {run.delivered && run.error && <p className="agent-chat-system" role="status">{run.error}</p>}
    {!run.delivered && (run.status === "queued" || run.status === "running") && <p className="agent-chat-progress" role="status">子 Agent 正在处理，完成后交付主 Agent。</p>}
    {!run.delivered && run.status === "completed" && <p className="agent-chat-progress" role="status">子 Agent 已完成，等待主 Agent 接收结果。</p>}
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
        <div className="agent-conversation-title">
          <span className={`agent-conversation-avatar agent-task-tone-${agentTone(task.agent_id)}`} aria-hidden="true">{agentInitials(title)}</span>
          <div><p className="agent-conversation-kicker">子智能体会话</p><h2>{title}</h2></div>
        </div>
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

/**
 * 显示主回答下的横向子智能体头像入口。
 *
 * @param tasks 当前回答尝试中可展示的子智能体摘要。
 * @param conversationId 所属会话 ID。
 * @param answerId 所属回答尝试 ID。
 * @returns 头像栏及按需打开的只读子智能体会话；没有子智能体时不渲染。
 */
export function AgentTasks({ tasks, conversationId, answerId }: { tasks: AgentTask[]; conversationId: string; answerId: string }) {
  const [selected, setSelected] = useState<AgentTask | null>(null);
  if (!tasks.length) return null;
  // 统计已完成任务，供栏头展示整体进度并避免用户逐个头像确认状态。
  const completedCount = tasks.filter(function isCompleted(task) { return task.status === "completed"; }).length;
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
  return <section className="agent-tasks" aria-label="子智能体">
    <div className="agent-tasks-heading">
      <span className="agent-tasks-title">子智能体</span>
      <span className="agent-tasks-summary" aria-live="polite">
        {completedCount === tasks.length ? (tasks.length === 1 ? "已完成" : `${tasks.length} 个均已完成`) : `${completedCount} / ${tasks.length} 已完成`}
      </span>
    </div>
    <div className="agent-task-avatar-row" role="list" aria-label="子智能体">
      {tasks.map(
        // 为每个子智能体保留稳定头像入口，并通过标题和状态说明其当前执行情况。
        function renderTask(task) {
          // 打开当前任务对应的只读子会话。
          function activateTask() { openTask(task); }
          const label = `${task.title} · ${statusLabel(task.status)}`;
          return <div className="agent-task-item" role="listitem" key={task.agent_id}>
            <button
              type="button"
              className={`agent-task-avatar-button agent-task-tone-${agentTone(task.agent_id)}`}
              onClick={activateTask}
              aria-label={label}
              aria-haspopup="dialog"
              aria-expanded={active?.agent_id === task.agent_id}
              title={label}
            >
              <span className="agent-task-avatar" aria-hidden="true">
                <span className="agent-task-avatar-initials">{agentInitials(task.title)}</span>
                <span className={`agent-task-state-dot agent-task-state-${task.status}`} />
              </span>
              <span className="agent-task-avatar-label">{task.title}</span>
            </button>
          </div>;
        },
      )}
    </div>
    {active && <AgentConversation task={active} conversationId={conversationId} answerId={answerId} onClose={closeTask} />}
  </section>;
}
