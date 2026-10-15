import { DocumentProgress } from './DocumentProgress';
import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
  type KeyboardEvent,
  type MouseEvent,
} from "react";
import {
  api,
  errorMessage,
  type Conversation,
  type ConversationDetail,
  type Message,
  type FileCitation,
  type Settings,
  type Usage,
} from "./api";
import { AttachmentPanel } from "./AttachmentPanel";
import { SearchCard } from "./SearchCard";
import { BudgetPrompt } from "./BudgetPrompt";
import { SettingsPanel } from "./SettingsPanel";
import { MemoryCandidates } from "./MemoryCandidates";
import { MemoryPanel } from "./MemoryPanel";

type Screen = "chat" | "settings" | "memory";

// 展示会话、记忆候选与正式动作、资料出处及预算状态，并隔离过期异步响应。
export function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversation, setConversation] = useState<ConversationDetail | null>(
    null,
  );
  const [settings, setSettings] = useState<Settings>({
    base_url: "",
    model: "",
    api_key_configured: false,
  });
  const [usage, setUsage] = useState<Usage[]>([]);
  const [screen, setScreen] = useState<Screen>("chat");
  const [draft, setDraft] = useState("");
  const [selectedAttachments, setSelectedAttachments] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [streamNotice, setStreamNotice] = useState("");
  const [changingRun, setChangingRun] = useState(false);
  const [pending, setPending] = useState<{
    id: string;
    content: string;
  } | null>(null);
  const [sourceMessage, setSourceMessage] = useState<string | null>(null);
  const activeId = useRef<string | null>(null);
  const selection = useRef(0);
  const bottom = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLFormElement>(null);

  // 当前会话最多有一条正在生成的回答。
  const generating = conversation?.messages.find(
    // 从持久化消息状态确定需要订阅的回答。
    (message) => message.status === "generating",
  );
  const paused = conversation?.messages.find(
    // 暂停尝试仍占用执行位置，用户恢复或取消前不能发送新的消息。
    (message) => message.status === "paused",
  );
  const activeRun = generating || paused;
  const generatingId = generating?.id;
  const conversationId = conversation?.id;

  useEffect(
    // 订阅持久化回答快照，重新进入会话时自动恢复当前进度。
    function subscribeToAnswer() {
      setStreamNotice("");
      if (!generatingId || !conversationId) return;
      const source = new EventSource(`/api/conversations/${conversationId}/messages/${generatingId}/events`);
      // 用服务器快照替换对应回答，不重复拼接重连后再次收到的内容。
      source.onmessage = (event: MessageEvent<string>) => {
        const message = JSON.parse(event.data) as Message;
        if (activeId.current !== conversationId) return;
        setStreamNotice("");
        setConversation(
          // 只更新本次订阅所属会话，防止切换后的旧事件覆盖新会话。
          (current) => current?.id === conversationId ? {
            ...current,
            messages: current.messages.map(
              // 用同一回答标识替换持久化快照。
              (item) => item.id === message.id ? message : item,
            ),
          } : current,
        );
        if (message.status !== "generating") source.close();
      };
      // 网络断开时保留内容并允许浏览器自动重连，不伪造模型失败或完成。
      source.onerror = () => {
        if (activeId.current === conversationId)
          setStreamNotice("进度连接中断，正在重新连接；后端回答状态将在连接恢复后同步。");
      };
      // 离开会话只取消界面订阅，停止生成由明确的停止按钮触发。
      return () => source.close();
    },
    [generatingId, conversationId],
  );

  useEffect(
    // 来源会话加载后定位原消息，直接滚动并标示该条来源。
    () => {
      if (sourceMessage && conversation?.messages.some(
        // 只定位当前已加载会话确实包含的消息标识。
        (message) => message.id === sourceMessage,
      )) document.getElementById(`message-${sourceMessage}`)?.scrollIntoView({ block: "center" });
    }, [sourceMessage, conversation],
  );

  // 打开后端登记的原消息，后续渲染负责定位，不依赖延时猜测。
  function openMemorySource(id: string, messageId: string) {
    setSourceMessage(messageId);
    void selectConversation(id);
  }

  // 切换时清空附件选择并递增请求序号，阻止旧资料或回答覆盖新会话。
  async function selectConversation(id: string) {
    const current = ++selection.current;
    activeId.current = id;
    localStorage.setItem("active-conversation", id);
    setScreen("chat");
    setConversation(null);
    setDraft("");
    setSelectedAttachments([]);
    setError("");
    try {
      const result = await api<ConversationDetail>(`/conversations/${id}`);
      if (current === selection.current) setConversation(result);
    } catch (failure) {
      if (current === selection.current) setError(errorMessage(failure));
    }
  }

  useEffect(
    // 首次进入时恢复公开配置、会话列表以及上一次打开的会话。
    function loadWorkspace() {
      let mounted = true;
      // 只在组件仍有效时应用初始化结果，避免卸载后更新状态。
      async function load() {
        try {
          const [items, saved] = await Promise.all([
            api<Conversation[]>("/conversations"),
            api<Settings>("/settings"),
          ]);
          if (!mounted) return;
          setConversations(items);
          setSettings(saved);
          const remembered = localStorage.getItem("active-conversation");
          // 仅恢复列表中仍然存在的会话。
          const target =
            items.find((item) => item.id === remembered) ?? items[0];
          if (target) await selectConversation(target.id);
        } catch (failure) {
          if (mounted) setError(errorMessage(failure));
        } finally {
          if (mounted) setLoading(false);
        }
      }
      void load();
      // 卸载时使尚未返回的加载结果失效。
      return () => {
        mounted = false;
        selection.current += 1;
      };
    },
    [],
  );

  useEffect(
    // 新消息或等待状态出现时，将聊天区滚动到最新内容。
    function revealLatestMessage() {
      bottom.current?.scrollIntoView({ behavior: "smooth" });
    },
    [conversation, pending],
  );

  // 新建持久化会话，成功后立即进入该会话并刷新左侧列表。
  async function createConversation(): Promise<Conversation | null> {
    setCreating(true);
    setError("");
    try {
      const item = await api<Conversation>("/conversations", {
        method: "POST",
        body: "{}",
      });
      setConversations(await api<Conversation[]>("/conversations"));
      await selectConversation(item.id);
      return item;
    } catch (failure) {
      setError(errorMessage(failure));
      return null;
    } finally {
      setCreating(false);
    }
  }

  // 响应新建按钮，异步错误由创建流程统一展示。
  function newConversation() {
    void createConversation();
  }

  // 从会话按钮的数据属性读取标识，避免为每项创建独立事件闭包。
  function openConversation(event: MouseEvent<HTMLButtonElement>) {
    const id = event.currentTarget.dataset.id;
    if (id) void selectConversation(id);
  }

  // 打开设置并取得最新用量；加载失败时保留明确错误。
  async function openSettings() {
    setScreen("settings");
    setError("");
    try {
      setUsage(await api<Usage[]>("/usage"));
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }

  // 打开记忆管理页，由页面重新核对正式事实与原消息来源。
  function openMemory() {
    setScreen("memory");
    setError("");
  }

  // 更新当前输入草稿。
  function editDraft(event: ChangeEvent<HTMLTextAreaElement>) {
    setDraft(event.target.value);
  }

  // Enter 发送、Shift+Enter 换行，中文输入法组合输入期间保持原行为。
  function composeKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      composer.current?.requestSubmit();
    }
  }

  // 将附件绑定到本条消息后启动流式生成，失败保留附件选择和未被新输入覆盖的草稿。
  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft.trim();
    if (
      !content ||
      pending ||
      activeRun ||
      changingRun ||
      creating ||
      loading ||
      !settings.api_key_configured
    )
      return;
    let id = activeId.current;
    if (!id) id = (await createConversation())?.id ?? null;
    if (!id) return;
    const targetId = id;
    setPending({ id: targetId, content });
    setDraft("");
    setError("");
    try {
      const result = await api<ConversationDetail>(
        `/conversations/${targetId}/runs`,
        { method: "POST", body: JSON.stringify({ content, attachment_ids: selectedAttachments }) },
      );
      if (activeId.current === targetId) {
        selection.current += 1;
        setConversation(result);
        setSelectedAttachments([]);
      }
    } catch (failure) {
      if (activeId.current === targetId) {
        setError(errorMessage(failure));
        setDraft(
          // 失败时恢复已提交问题，但不覆盖用户在等待期间输入的新草稿。
          (current) => current || content,
        );
      }
      try {
        const result = await api<ConversationDetail>(
          `/conversations/${targetId}`,
        );
        if (activeId.current === targetId) {
          selection.current += 1;
          setConversation(result);
        }
      } catch {
        /* 主请求错误已展示，保留当前已知内容。 */
      }
    } finally {
      setPending(null);
      try {
        setConversations(await api<Conversation[]>("/conversations"));
      } catch (failure) {
        setError(errorMessage(failure));
      }
    }
  }

  // 对当前回答发送停止或重试操作，并重新应用后端保存的真实状态。
  async function changeRun(event: MouseEvent<HTMLButtonElement>) {
    const id = activeId.current;
    const messageId = event.currentTarget.dataset.message;
    const action = event.currentTarget.dataset.action;
    const approve = event.currentTarget.dataset.approve === "true";
    if (!id || !messageId || !action || changingRun) return;
    setChangingRun(true);
    setError("");
    try {
      if (approve && paused?.budget_pause) {
        await api(`/budget/operations/${paused.budget_pause.operation_id}/approve`, { method: "POST", body: JSON.stringify({ pause_id: paused.budget_pause.pause_id }) });
      }
      const result = await api<ConversationDetail>(`/conversations/${id}/messages/${messageId}/${action}`, { method: "POST" });
      if (activeId.current === id) {
        selection.current += 1;
        setConversation(result);
      }
    } catch (failure) {
      if (activeId.current === id) setError(errorMessage(failure));
    } finally {
      setChangingRun(false);
    }
  }

  // 候选操作后读取当前会话，保留草稿并防止切换后的旧结果覆盖新会话。
  async function refreshConversation() {
    const id = activeId.current;
    if (!id) return;
    const result = await api<ConversationDetail>(`/conversations/${id}`);
    if (activeId.current === id) setConversation(result);
  }

  // 将每个会话渲染为可访问的切换按钮。
  function renderConversation(item: Conversation) {
    return (
      <button
        key={item.id}
        className={`conversation-button ${activeId.current === item.id && screen === "chat" ? "active" : ""}`}
        data-id={item.id}
        onClick={openConversation}
        title={item.title}
      >
        <span className="conversation-icon" aria-hidden="true">
          ◷
        </span>
        <span>{item.title}</span>
      </button>
    );
  }

  // 仅更新当前会话的确认结果，切换后的旧请求不能覆盖新会话。
  function applySearchUpdate(detail: ConversationDetail) {
    if (activeId.current === detail.id) { selection.current += 1; setConversation(detail); }
  }

  // 只渲染后端提供的安全网页来源链接，模型正文仍按纯文本展示。
  function renderWebSource(source: Message["web_sources"][number]) {
    return <details className="file-citation" key={source.key}><summary>[{source.key}] <a href={source.url} target="_blank" rel="noopener noreferrer">{source.title || source.url}</a></summary><blockquote>{source.excerpt}</blockquote></details>;
  }

  // 展示后端核验的文件位置与原文摘录，所有正文均按纯文本渲染。
  function renderCitation(citation: FileCitation) {
    return <details className="file-citation" key={citation.key} open><summary>[{citation.key}] <a href={`/api/conversations/${conversation?.id}/attachments/${citation.attachment_id}/file`} download>{citation.filename} · {citation.location}</a></summary><blockquote>{citation.excerpt}</blockquote></details>;
  }

  // 展示回答、候选与搜索外发确认、正式记忆及全文进度，保留出处、暂停和重试入口。
  function renderMessage(message: Message) {
    return (
      <article
        key={message.id}
        id={`message-${message.id}`}
        className={`message ${message.role} ${message.status} ${sourceMessage === message.id ? "source-highlight" : ""}`}
      >
        <div className="message-label">
          {message.role === "user" ? "你" : "助手"}
          <time>
            {new Date(message.created_at).toLocaleTimeString("zh-CN", {
              hour: "2-digit",
              minute: "2-digit",
            })}
          </time>
        </div>
        <div className="message-content">{message.content}</div>
        {message.document_progress && <DocumentProgress progress={message.document_progress} status={message.status} />}
        {message.memory_recall && message.memory_recall.status !== "skipped" && <div data-testid="memory-recall" className={`memory-recall ${message.memory_recall.status}`} role="status">{message.memory_recall.detail}</div>}
        {message.memory_update && <div className={`memory-action ${message.memory_update.status}`} role="status">
          {message.memory_update.status === "deleted" ? "相关长期记忆已忘记；原聊天仍保留。" : message.memory_update.status === "complete" ? `已更正 · ${message.memory_update.fact}` : `记忆更正未完成 · ${message.memory_update.error || "正在同步；旧事实已停止召回。"}`}
        </div>}
        {message.memory_action && <div className={`memory-action ${message.memory_action.status}`} role="status">
          {message.memory_action.status === "deleted" ? "这条长期记忆已忘记；原聊天仍保留。" : message.memory_action.status === "deleting" ? "记忆已停止召回，清理尚未完成。" : message.memory_action.status === "complete" ? `已保存 · ${message.memory_action.fact}` : message.memory_action.status === "failed" ? `记忆保存未完成 · ${message.memory_action.error || "可以重试核对。"}` : "记忆正在处理，尚未确认保存。"}
        </div>}
        {message.role === "assistant" && conversation && <MemoryCandidates message={message} conversationId={conversation.id} onChanged={refreshConversation} />}
        {message.status === "paused" && message.budget_pause && <section className="run-budget" aria-label="回答等待预算">
          <strong>后续云端请求已暂停</strong><p>{message.budget_pause.detail}</p><p>已完成的记忆和资料处理会保留。批准仅允许下一次调用。</p>
          <button className="retry-button" data-action="resume" data-approve="true" data-message={message.id} onClick={changeRun} disabled={changingRun}>仅允许下一次云端调用并继续</button>
          <button className="retry-button" data-action="resume" data-message={message.id} onClick={changeRun} disabled={changingRun}>重新检查并继续</button>
          <button className="retry-button" data-action="stop" data-message={message.id} onClick={changeRun} disabled={changingRun}>取消本次回答</button>
        </section>}
        {!!message.attachment_ids?.length && <div className="message-progress">已附 {message.attachment_ids.length} 个会话文件</div>}
        {message.search_action && conversation && <SearchCard message={message} conversationId={conversation.id} onUpdated={applySearchUpdate} />}
        {message.web_sources?.map(renderWebSource)}
        {message.citations?.map(renderCitation)}
        {message.status === "generating" && <div className="message-progress" role="status">正在生成…</div>}
        {message.status === "failed" && (
          <div className="message-error" role="alert">回答未完成 · {message.error || "可以重试或检查设置。"}</div>
        )}
        {message.status === "stopped" && <div className="message-progress">已停止 · 已保留部分内容</div>}
        {(message.status === "failed" || message.status === "stopped") && conversation?.messages.at(-1)?.id === message.id && (
          <button className="retry-button" data-action="retry" data-message={message.id} onClick={changeRun} disabled={changingRun || !!pending}>
            重试回答
          </button>
        )}
      </article>
    );
  }

  const currentPending = pending?.id === activeId.current ? pending : null;
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            ✳
          </span>
          <div>
            <strong>对话</strong>
            <span>留一点空间，给想法。</span>
          </div>
        </div>
        <button
          className="new-conversation"
          onClick={newConversation}
          disabled={creating || loading}
        >
          <span aria-hidden="true">＋</span>新建会话
        </button>
        <div className="section-label">
          你的会话 <span>{conversations.length || ""}</span>
        </div>
        <nav className="conversation-list" aria-label="会话列表">
          {conversations.map(renderConversation)}
          {!conversations.length && (
            <p className="sidebar-empty">新的想法，从这里开始。</p>
          )}
        </nav>
        <div className="sidebar-bottom">
          <button
            className={`nav-button ${screen === "memory" ? "selected" : ""}`}
            onClick={openMemory}
          >
            <span aria-hidden="true">◇</span>长期记忆
          </button>
          <button
            className={`nav-button ${screen === "settings" ? "selected" : ""}`}
            onClick={openSettings}
          >
            <span aria-hidden="true">⚙</span>设置
          </button>
          <div className="local-note">
            <span className="status-dot" />
            本机保存 · 云端回答
          </div>
        </div>
      </aside>
      <main className="main-panel">
        <header className="topbar">
          <div>
            <h1>个人对话空间</h1>
            <span className="topbar-divider">/</span>
            <span className="topbar-subtitle">
              {screen === "settings"
                ? "设置"
                : screen === "memory"
                  ? "长期记忆"
                  : "日常、灵感与思考"}
            </span>
          </div>
          <button className="model-pill" onClick={openSettings}>
            <span
              className={`status-dot ${settings.api_key_configured ? "configured" : ""}`}
            />
            {settings.model || "配置模型"}
            <span aria-hidden="true">⌄</span>
          </button>
        </header>
        {error && (
          <div className="global-error" role="alert">
            {error}
          </div>
        )}
        <BudgetPrompt onConfigure={openSettings} />
        {screen === "settings" ? (
          <div className="page-scroll">
            <SettingsPanel
              settings={settings}
              usage={usage}
              onSaved={setSettings}
            />
          </div>
        ) : screen === "memory" ? (
          <MemoryPanel onSource={openMemorySource} />
        ) : (
          <div className="chat-layout">
            <div className="chat-scroll">
              <div className="chat-content">
                {loading ? (
                  <div className="empty-state">
                    <p>正在打开你的空间…</p>
                  </div>
                ) : !conversation?.messages.length && !currentPending ? (
                  <section className="empty-state">
                    <div className="hello-mark" aria-hidden="true">
                      ✳
                    </div>
                    <div className="eyebrow">从一个问题开始</div>
                    <h2>今天，想聊些什么？</h2>
                    <p>
                      整理思路、写一段文字，
                      <br />
                      或把脑海里还没成形的想法说出来。
                    </p>
                    {!settings.api_key_configured && (
                      <button className="setup-link" onClick={openSettings}>
                        先连接你的模型 <span aria-hidden="true">↗</span>
                      </button>
                    )}
                    <div className="topic-tags">
                      <span>日常问答</span>
                      <span>写作灵感</span>
                      <span>梳理想法</span>
                    </div>
                  </section>
                ) : (
                  <div className="messages">
                    {conversation?.messages.map(renderMessage)}
                    {currentPending && (
                      <>
                        <article className="message user">
                          <div className="message-label">你</div>
                          <div className="message-content">
                            {currentPending.content}
                          </div>
                        </article>
                        <div className="thinking" role="status">
                          <span className="thinking-dot" />
                          正在等待回答…
                        </div>
                      </>
                    )}
                  </div>
                )}
                <div ref={bottom} />
              </div>
            </div>
            <div className="composer-wrap">
              <AttachmentPanel conversationId={activeId.current} selected={selectedAttachments} onSelect={setSelectedAttachments} disabled={!!pending || !!generating || changingRun || creating || loading} />
              {streamNotice && <p className="stream-notice" role="status">{streamNotice}</p>}
              <form className="composer" ref={composer} onSubmit={send}>
                <textarea
                  aria-label="消息"
                  placeholder={
                    settings.api_key_configured
                      ? "把你的想法写在这里…"
                      : "先在设置中连接一个模型，即可开始对话"
                  }
                  value={draft}
                  onChange={editDraft}
                  onKeyDown={composeKey}
                  maxLength={32000}
                  disabled={!settings.api_key_configured || loading || creating}
                  rows={2}
                />
                <div className="composer-bottom">
                  <span>
                    文字对话 <i>·</i> Shift + Enter 换行
                  </span>
                  {activeRun ? (
                    <button type="button" className="stop-button" data-action="stop" data-message={activeRun.id} onClick={changeRun} disabled={changingRun}>
                      停止生成
                    </button>
                  ) : <button
                    className="send-button"
                    aria-label="发送消息"
                    disabled={
                      !draft.trim() ||
                      !!pending ||
                      creating ||
                      loading ||
                      !settings.api_key_configured
                    }
                  >
                    <span aria-hidden="true">↑</span>
                  </button>}
                </div>
              </form>
              <p className="composer-note">回答可能有误，请核对重要信息。停止不一定撤销服务商费用；已返回用量仍会记录。</p>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
