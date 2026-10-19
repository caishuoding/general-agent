import { SourceDeletion, SourceDeletionPending, type DeleteTarget } from './SourceDeletion';
import { AnswerContent } from "./AnswerContent";
import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
  type KeyboardEvent,
  type MouseEvent,
  type UIEvent,
} from "react";
import {
  api,
  errorMessage,
  type AuthUser,
  type Conversation,
  type ConversationDetail,
  type Message,
  type MessageAttachment,
} from "./api";
import { AttachmentPanel } from "./AttachmentPanel";
import { LoginScreen, PasswordChangeScreen } from "./AuthScreens";

type Screen = "chat" | "password";

// 展示会话、消息内附件与来源删除状态，并隔离过期异步响应。
export function App() {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversation, setConversation] = useState<ConversationDetail | null>(
    null,
  );
  const [screen, setScreen] = useState<Screen>("chat");
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [streamNotice, setStreamNotice] = useState("");
  const [changingRun, setChangingRun] = useState(false);
  const [pending, setPending] = useState<{
    id: string;
    content: string;
  } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null);
  const [sourceRevision, setSourceRevision] = useState(0);
  const [attachmentReset, setAttachmentReset] = useState(0);
  const activeId = useRef<string | null>(null);
  const selection = useRef(0);
  const bottom = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  const [showLatest, setShowLatest] = useState(false);
  const composer = useRef<HTMLFormElement>(null);

  // 当前会话最多有一条正在生成的回答。
  const generating = conversation?.messages.find(
    // 从持久化消息状态确定需要订阅的回答。
    (message) => message.status === "generating",
  );
  const activeRun = generating;
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

  // 切换时隔离旧响应与附件，避免过期请求覆盖当前会话。
  async function selectConversation(id: string) {
    const current = ++selection.current;
    activeId.current = id;
    localStorage.setItem("active-conversation", id);
    followLatest.current = true;
    setShowLatest(false);
    setScreen("chat");
    setConversation(null);
    setDraft("");
    setError("");
    try {
      const result = await api<ConversationDetail>(`/conversations/${id}`);
      if (current === selection.current) {
        setConversation(result);
      }
    } catch (failure) {
      if (current === selection.current) setError(errorMessage(failure));
    }
  }

  useEffect(
    // 首次进入时读取服务端登录会话，未登录时只显示登录页。
    function restoreSession() {
      let mounted = true;
      // 只在组件仍有效时应用会话恢复结果。
      async function load() {
        try {
          const current = await api<AuthUser>("/auth/me");
          if (!mounted) return;
          if (current.role === "admin") {
            window.location.replace("/admin");
            return;
          }
          setUser(current);
        } catch {
          if (mounted) setUser(null);
        } finally {
          if (mounted) setAuthReady(true);
        }
      }
      void load();
      // 卸载后丢弃迟到的会话结果。
      return () => {
        mounted = false;
      };
    },
    [],
  );

  useEffect(
    // 登录后恢复会话列表；普通用户不请求仅管理员可见的配置。
    function loadWorkspace() {
      if (!authReady || !user || user.must_change_password) {
        setLoading(false);
        return;
      }
      let mounted = true;
      // 只在组件仍有效时应用初始化结果，避免卸载后更新状态。
      async function load() {
        try {
          const items = await api<Conversation[]>("/conversations");
          if (!mounted) return;
          setConversations(items);
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
      setLoading(true);
      void load();
      // 卸载或切换账号时使尚未返回的加载结果失效。
      return () => {
        mounted = false;
        selection.current += 1;
      };
    },
    [authReady, user?.id, user?.must_change_password, user?.role],
  );

  useEffect(
    /** 仅在用户停留在底部时跟随流式回答，保留上滑后的阅读位置。 */
    function revealLatestMessage() {
      if (followLatest.current) bottom.current?.scrollIntoView({ block: "end" });
    },
    [conversation, pending],
  );

  /** 根据会话滚动容器的位置更新跟随状态，无返回值；上滑时显示返回入口。 */
  function trackReadingPosition(event: UIEvent<HTMLDivElement>) {
    const element = event.currentTarget;
    const nearBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
    followLatest.current = nearBottom;
    setShowLatest(!nearBottom);
  }

  /** 用户明确请求最新内容时恢复跟随并定位底部，无输入或返回值。 */
  function returnToLatest() {
    followLatest.current = true;
    setShowLatest(false);
    bottom.current?.scrollIntoView({ block: "end" });
  }

  // 清理后刷新来源；详情只回写发起时仍选中的会话，切换或重新选择后的旧响应失效。
  async function refreshSources() {
    const id = activeId.current;
    const selected = selection.current;
    const items = await api<Conversation[]>("/conversations");
    setConversations(items);
    setSourceRevision(
      // 每次清理后重新载入附件和未完成清理记录。
      (value) => value + 1,
    );
    if (!id || activeId.current !== id || selection.current !== selected) return;
    if (items.some(
      // 只刷新发起删除时仍有效的原会话，不读取用户后来选择的会话。
      (item) => item.id === id,
    )) {
      const detail = await api<ConversationDetail>(`/conversations/${id}`);
      if (activeId.current === id && selection.current === selected) setConversation(detail);
    } else { activeId.current = null; selection.current += 1; setConversation(null); localStorage.removeItem("active-conversation"); }
  }
  // 当前会话删除需先呈现包含更正和附件来源的完整影响。
  function deleteConversation() { if (activeId.current) setDeleteTarget({ path: `/conversations/${activeId.current}`, label: '会话' }); }
  // 附件按独立上传身份预览，绝不通过名称推断删除范围。
  function deleteAttachment(id: string, name: string) { if (activeId.current) setDeleteTarget({ path: `/conversations/${activeId.current}/attachments/${id}`, label: `附件 ${name}` }); }
  // 关闭预览不执行任何删除或保留授权。
  function closeDeletion() { setDeleteTarget(null); }

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

  // 结束服务端会话并清空前端中的账号相关状态。
  async function logout() {
    try {
      await api("/auth/logout", { method: "POST" });
    } catch {
      /* 即使网络失败，也回到未登录界面，由下一次请求确认会话状态。 */
    } finally {
      activeId.current = null;
      selection.current += 1;
      localStorage.removeItem("active-conversation");
      setUser(null);
      setConversations([]);
      setConversation(null);
      setScreen("chat");
      setError("");
      setDraft("");
    }
  }

  // 登录成功后保存当前账号并触发工作区加载。
  function handleAuthenticated(next: AuthUser) {
    setError("");
    if (next.role === "admin") {
      window.location.replace("/admin");
      return;
    }
    setUser(next);
  }

  // 强制改密成功后更新当前账号状态。
  function handlePasswordChanged(next: AuthUser) {
    setError("");
    setUser(next);
    setScreen("chat");
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

  // 启动流式生成；当前会话附件会自动进入后端上下文，失败时保留草稿。
  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft.trim();
    if (
      !content ||
      pending ||
      activeRun ||
      changingRun ||
      creating ||
      loading
    )
      return;
    let id = activeId.current;
    if (!id) id = (await createConversation())?.id ?? null;
    if (!id) return;
    const targetId = id;
    followLatest.current = true;
    setShowLatest(false);
    setPending({ id: targetId, content });
    setDraft("");
    setError("");
    try {
      const result = await api<ConversationDetail>(
        `/conversations/${targetId}/runs`,
        { method: "POST", body: JSON.stringify({ content }) },
      );
      if (activeId.current === targetId) {
        selection.current += 1;
        setConversation(result);
        setAttachmentReset(
          // 发送成功后清空输入区托盘，附件只保留在对应消息中。
          (value) => value + 1,
        );
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
    if (!id || !messageId || !action || changingRun) return;
    setChangingRun(true);
    setError("");
    try {
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

  /** 根据附件处理状态返回聊天记录中的简短说明，无输入返回展示文字。 */
  function attachmentLabel(attachment: MessageAttachment) {
    if (attachment.status === "ready") return "可用";
    if (attachment.status === "failed") return "解析失败";
    if (attachment.status === "deleting") return "已删除";
    return "处理中";
  }

  /** 渲染用户消息随附文件的下载入口，保留原始文件名和处理状态。 */
  function renderMessageAttachments(attachments: MessageAttachment[]) {
    return (
      <ul className="message-attachments" aria-label="本条消息附件">
        {attachments.map((attachment) => (
          <li key={attachment.id}>
            <a
              className={`message-attachment ${attachment.status}`}
              href={`/api/conversations/${conversation?.id}/attachments/${attachment.id}/file`}
              download
              aria-label={`下载附件 ${attachment.filename}`}
            >
              <span className="message-attachment-name">{attachment.filename}</span>
              <small>{attachmentLabel(attachment)}</small>
            </a>
          </li>
        ))}
      </ul>
    );
  }

  /** 接收持久化消息并返回消息界面；助手显示 Markdown，用户保留原始输入。 */
  function renderMessage(message: Message) {
    return (
      <article
        key={message.id}
        id={`message-${message.id}`}
        className={`message ${message.role} ${message.status}`}
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
        {message.role === "assistant"
          ? <AnswerContent message={message} />
          : <div className="message-content">
              <span>{message.content}</span>
              {!!message.attachments.length && renderMessageAttachments(message.attachments)}
            </div>}
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
  if (!authReady) {
    return <div className="auth-loading" role="status">正在检查登录状态…</div>;
  }
  if (!user) {
    return (
      <LoginScreen
        onAuthenticated={handleAuthenticated}
      />
    );
  }
  if (user.must_change_password) {
    return (
      <PasswordChangeScreen
        user={user}
        onChanged={handlePasswordChanged}
        onLogout={logout}
      />
    );
  }
  if (screen === "password") {
    return (
      <PasswordChangeScreen
        user={user}
        forced={false}
        onChanged={handlePasswordChanged}
        onLogout={logout}
        onCancel={() => setScreen("chat")}
      />
    );
  }
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
          <div className="user-menu">
            <span className="user-name">{user.username}</span>
            <button className="logout-button" onClick={() => setScreen("password")}>
              修改密码
            </button>
            <button className="logout-button" onClick={logout}>
              退出
            </button>
          </div>
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
            <span className="topbar-subtitle">日常、灵感与思考</span>
          </div>
          <span className="user-pill">{user.username}</span>
        </header>
        {error && (
          <div className="global-error" role="alert">
            {error}
          </div>
        )}
        <SourceDeletionPending revision={sourceRevision} onChanged={refreshSources} />
        {deleteTarget && <SourceDeletion key={deleteTarget.path} target={deleteTarget} onClose={closeDeletion} onChanged={refreshSources} />}
        {screen === 'chat' && conversation && <button className="delete-conversation" onClick={deleteConversation}>删除当前会话</button>}
        <div className="chat-layout">
            <div className="chat-scroll" onScroll={trackReadingPosition}>
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
                            <span>{currentPending.content}</span>
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
            {showLatest && <button type="button" className="return-to-latest" onClick={returnToLatest}>↓ 回到最新</button>}
            <div className="composer-wrap">
              {streamNotice && <p className="stream-notice" role="status">{streamNotice}</p>}
              <form className="composer" ref={composer} onSubmit={send}>
                <AttachmentPanel revision={sourceRevision} resetToken={attachmentReset} onDelete={deleteAttachment} conversationId={activeId.current} disabled={!!pending || !!generating || changingRun || creating || loading} />
                <textarea
                  aria-label="消息"
                  placeholder="把你的想法写在这里…"
                  value={draft}
                  onChange={editDraft}
                  onKeyDown={composeKey}
                  maxLength={32000}
                  disabled={loading || creating}
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
                      loading
                    }
                  >
                    <span aria-hidden="true">↑</span>
                  </button>}
                </div>
              </form>
              <p className="composer-note">回答可能有误，请核对重要信息。停止生成不会撤回已经发出的服务请求。</p>
            </div>
        </div>
      </main>
    </div>
  );
}
