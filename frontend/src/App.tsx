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
  type Settings,
  type Usage,
} from "./api";
import { SettingsPanel } from "./SettingsPanel";

type Screen = "chat" | "settings" | "memory";

// 展示会话、文字对话、配置与当前能力边界，并隔离异步切换带来的过期响应。
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
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [pending, setPending] = useState<{
    id: string;
    content: string;
  } | null>(null);
  const activeId = useRef<string | null>(null);
  const selection = useRef(0);
  const bottom = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLFormElement>(null);

  // 切换会话时递增请求序号，阻止较慢的旧请求覆盖当前会话。
  async function selectConversation(id: string) {
    const current = ++selection.current;
    activeId.current = id;
    localStorage.setItem("active-conversation", id);
    setScreen("chat");
    setConversation(null);
    setDraft("");
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

  // 记忆入口只说明当前能力，避免将尚未实现的功能呈现为可用。
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

  // 发送当前会话的一条消息，切换会话后返回的结果不会覆盖新页面。
  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft.trim();
    if (
      !content ||
      pending ||
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
        `/conversations/${targetId}/messages`,
        { method: "POST", body: JSON.stringify({ content }) },
      );
      if (activeId.current === targetId) setConversation(result);
    } catch (failure) {
      if (activeId.current === targetId) setError(errorMessage(failure));
      try {
        const result = await api<ConversationDetail>(
          `/conversations/${targetId}`,
        );
        if (activeId.current === targetId) setConversation(result);
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

  // 以纯文本展示消息，保留换行并明确区分失败状态，避免执行模型返回的 HTML。
  function renderMessage(message: Message) {
    return (
      <article
        key={message.id}
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
        <div className="message-content">{message.content}</div>
        {message.status === "failed" && (
          <div className="message-error">回答未完成 · 可检查设置后重新发送</div>
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
        {screen === "settings" ? (
          <div className="page-scroll">
            <SettingsPanel
              settings={settings}
              usage={usage}
              onSaved={setSettings}
            />
          </div>
        ) : screen === "memory" ? (
          <section className="memory-page">
            <div className="memory-symbol" aria-hidden="true">
              ◇
            </div>
            <div className="eyebrow">关于你的长期记忆</div>
            <h2>长期记忆尚未启用</h2>
            <p>
              当前对话只使用本会话中的消息。
              <br />
              跨会话记忆将在后续版本提供。
            </p>
          </section>
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
                  disabled={!settings.api_key_configured || loading}
                  rows={2}
                />
                <div className="composer-bottom">
                  <span>
                    文字对话 <i>·</i> Shift + Enter 换行
                  </span>
                  <button
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
                  </button>
                </div>
              </form>
              <p className="composer-note">回答可能有误，请核对重要信息。</p>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
