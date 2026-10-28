import { SourceDeletion, SourceDeletionPending, type DeleteTarget } from './SourceDeletion';
import { AgentProgress } from "./AgentProgress";
import { AgentTasks } from "./AgentTasks";
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
  AUTH_EXPIRED_EVENT,
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
import { KnowledgePanel, KnowledgeSelector } from './KnowledgePanel';

type Screen = "chat" | "password" | "knowledge";

// 展示响应式会话与独立知识库入口，保持附件和知识库选择各自的业务边界。
export function App() {
  const [mobile, setMobile] = useState(window.matchMedia("(max-width: 800px)").matches);
  const [navigationOpen, setNavigationOpen] = useState(false);
  const navigation = useRef<HTMLElement>(null);
  const navigationTrigger = useRef<HTMLButtonElement>(null);
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
    startedAt: string;
  } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null);
  const [sourceRevision, setSourceRevision] = useState(0);
  const [attachmentReset, setAttachmentReset] = useState(0);
  const activeId = useRef<string | null>(null);
  const selection = useRef(0);
  const chatScroll = useRef<HTMLDivElement>(null);
  const draftInput = useRef<HTMLTextAreaElement>(null);
  const composing = useRef(false);
  const compositionEndedAt = useRef(0);
  const followLatest = useRef(true);
  const [showLatest, setShowLatest] = useState(false);
  const composer = useRef<HTMLFormElement>(null);

  useEffect(
    // 手机聊天区跟随可视视口高度和偏移，阻止软键盘把整个文档推离屏幕。
    function watchVisibleViewport() {
      if (!mobile || !user || user.must_change_password || screen !== "chat") return;
      const viewport = window.visualViewport;
      const root = document.documentElement;
      let frame = 0;
      root.classList.add("chat-workspace-open");
      // 同步键盘动画和浏览器平移；保持原有跟随状态，不打断历史阅读。
      function updateViewport() {
        frame = 0;
        if (viewport && viewport.scale !== 1) return;
        const height = viewport?.height ?? window.innerHeight;
        root.style.setProperty("--workspace-height", `${height}px`);
        root.style.setProperty("--workspace-top", `${viewport?.offsetTop ?? 0}px`);
        root.classList.toggle("chat-keyboard-open", window.innerHeight - height > 120);
        if (followLatest.current) scrollToLatest();
      }
      // 同一帧的 resize、scroll 和输入焦点变化合并处理，避免布局抖动。
      function scheduleViewportUpdate() {
        if (!frame) frame = requestAnimationFrame(updateViewport);
      }
      updateViewport();
      viewport?.addEventListener("resize", scheduleViewportUpdate);
      viewport?.addEventListener("scroll", scheduleViewportUpdate);
      window.addEventListener("resize", scheduleViewportUpdate);
      // 输入区换行或附件托盘变化后，仅在原本跟随最新消息时重新贴底。
      const observer = new ResizeObserver(scheduleViewportUpdate);
      if (chatScroll.current) observer.observe(chatScroll.current);
      if (composer.current) observer.observe(composer.current);
      // 离开聊天页时恢复普通文档滚动，保留登录及改密页的自然滚动行为。
      return () => {
        cancelAnimationFrame(frame);
        observer.disconnect();
        viewport?.removeEventListener("resize", scheduleViewportUpdate);
        viewport?.removeEventListener("scroll", scheduleViewportUpdate);
        window.removeEventListener("resize", scheduleViewportUpdate);
        root.classList.remove("chat-workspace-open", "chat-keyboard-open");
        root.style.removeProperty("--workspace-height");
        root.style.removeProperty("--workspace-top");
      };
    },
    [mobile, user?.id, user?.must_change_password, screen],
  );

  useEffect(
    // 同步布局断点，切回桌面时清除手机抽屉状态。
    function watchLayout() {
      const query = window.matchMedia("(max-width: 800px)");
      // 断点变化后恢复默认导航，避免隐藏抽屉保留焦点。
      function updateLayout() {
        setMobile(query.matches);
        setNavigationOpen(false);
      }
      query.addEventListener("change", updateLayout);
      // 卸载时移除媒体查询监听。
      return () => query.removeEventListener("change", updateLayout);
    },
    [],
  );

  useEffect(
    // 手机抽屉打开后限制键盘焦点，并支持 Escape 关闭和焦点返回。
    function manageNavigationFocus() {
      if (!mobile || !navigationOpen) return;
      const panel = navigation.current;
      panel?.querySelector<HTMLButtonElement>("button")?.focus();
      // 在抽屉内循环 Tab，防止键盘进入被遮挡的会话区。
      function handleKey(event: globalThis.KeyboardEvent) {
        if (event.key === "Escape") {
          setNavigationOpen(false);
          return;
        }
        if (event.key !== "Tab") return;
        const buttons = panel?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)");
        if (!buttons?.length) return;
        const first = buttons[0];
        const last = buttons[buttons.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
      document.addEventListener("keydown", handleKey);
      // 关闭或卸载抽屉时释放监听，并把焦点还给入口。
      return () => {
        document.removeEventListener("keydown", handleKey);
        navigationTrigger.current?.focus();
      };
    },
    [mobile, navigationOpen],
  );

  // 打开手机端会话导航。
  function openNavigation() { setNavigationOpen(true); }
  // 关闭手机端会话导航，不改变当前会话。
  function closeNavigation() { setNavigationOpen(false); }
  // 从会话导航进入改密页，并关闭手机抽屉。
  function openPasswordScreen() { setNavigationOpen(false); setScreen("password"); }
  // 返回聊天工作区。
  function returnToChat() { setScreen("chat"); }

  /** 打开独立知识库管理页，同时关闭手机导航抽屉。 */
  function openKnowledge() { setNavigationOpen(false); setScreen('knowledge'); }

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
      // 离开会话只取消界面订阅，停止回答由明确的按钮触发。
      return () => source.close();
    },
    [generatingId, conversationId],
  );

  // 切换时关闭手机导航并隔离旧响应与附件，避免过期请求覆盖当前会话。
  async function selectConversation(id: string) {
    setNavigationOpen(false);
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
    // 首次进入时读取登录会话；确认普通用户后先进入加载态，避免聊天区短暂可用。
    function restoreSession() {
      let mounted = true;
      // 只在组件仍有效时应用会话恢复结果。
      async function load() {
        try {
          const current = await api<AuthUser>("/auth/me");
          if (!mounted) return;
          setLoading(true);
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
    // 监听会话失效广播，避免会话过期后继续显示聊天界面。
    function watchSessionExpiry() {
      // 会话失效时清空账号并回到用户登录页。
      function handleExpired() {
        setNavigationOpen(false);
        setUser(null);
        setScreen("chat");
      }
      window.addEventListener(AUTH_EXPIRED_EVENT, handleExpired);
      return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handleExpired);
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
      if (followLatest.current) scrollToLatest();
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

  // 仅滚动消息容器，避免 scrollIntoView 连带滚动手机浏览器的文档和视口。
  function scrollToLatest() {
    const element = chatScroll.current;
    if (element) element.scrollTop = element.scrollHeight;
  }

  /** 用户明确请求最新内容时恢复跟随并定位消息容器底部。 */
  function returnToLatest() {
    followLatest.current = true;
    setShowLatest(false);
    scrollToLatest();
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
  // 先关闭手机导航，再展示当前会话的删除确认，避免对话框被抽屉遮挡。
  function deleteConversation() {
    if (!activeId.current) return;
    setNavigationOpen(false);
    setDeleteTarget({ path: `/conversations/${activeId.current}`, label: '会话' });
  }
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

  /** 将示例问题放入输入框并聚焦，先让用户确认或补充，不自动发送。 */
  function useExamplePrompt(event: MouseEvent<HTMLButtonElement>) {
    const prompt = event.currentTarget.dataset.prompt;
    if (!prompt) return;
    setDraft(prompt);
    // 等草稿写入后聚焦输入框，方便用户继续修改。
    requestAnimationFrame(function focusComposer() {
      draftInput.current?.focus();
    });
  }

  /** 关闭会话操作菜单后再打开删除确认，避免菜单和确认层互相遮挡。 */
  function deleteConversationFromMenu(event: MouseEvent<HTMLButtonElement>) {
    event.currentTarget.closest("details")?.removeAttribute("open");
    deleteConversation();
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
      setNavigationOpen(false);
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

  // 登录成功后先进入工作区加载态，再保存账号，避免暴露未初始化的聊天控件。
  function handleAuthenticated(next: AuthUser) {
    setError("");
    setLoading(true);
    setUser(next);
  }

  // 强制改密成功后进入加载态并更新账号，确保聊天区只在工作区就绪后开放。
  function handlePasswordChanged(next: AuthUser) {
    setError("");
    setLoading(true);
    setUser(next);
    setScreen("chat");
  }

  // 更新草稿并让输入框随内容增高；长草稿在输入框内部滚动。
  function editDraft(event: ChangeEvent<HTMLTextAreaElement>) {
    setDraft(event.target.value);
    resizeDraft(event.currentTarget);
  }

  // 以实际内容测量输入高度，最多占用 120px，给消息区保留阅读空间。
  function resizeDraft(element: HTMLTextAreaElement) {
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 120)}px`;
  }

  useEffect(
    // 发送清空或失败恢复草稿时同步输入框高度。
    function syncDraftHeight() {
      if (draftInput.current) resizeDraft(draftInput.current);
    },
    [draft, user?.id, screen, mobile],
  );

  // 触屏点击发送或停止时保留输入焦点，避免键盘反复收起和弹出。
  function preserveComposerFocus(event: React.PointerEvent<HTMLDivElement>) {
    const target = event.target as HTMLElement;
    if (event.pointerType === "touch" && document.activeElement === draftInput.current && target.closest("button")) event.preventDefault();
  }

  /** 标记输入法开始组字，后续回车只处理候选而不是发送消息。 */
  function startComposition() {
    composing.current = true;
  }

  /** 记录输入法结束时间，拦截 Safari 等浏览器紧随组字结束送达的回车。 */
  function finishComposition() {
    composing.current = false;
    compositionEndedAt.current = performance.now();
  }

  // 桌面 Enter 发送、Shift+Enter 换行；输入法组字和候选确认回车绝不发送。
  function composeKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    const native = event.nativeEvent;
    const composingNow = composing.current || native.isComposing || native.keyCode === 229;
    const justFinishedComposition = performance.now() - compositionEndedAt.current < 120;
    if (
      event.key === "Enter" &&
      !window.matchMedia("(pointer: coarse)").matches &&
      !event.shiftKey &&
      !event.repeat &&
      !composingNow &&
      !justFinishedComposition
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
    setPending({ id: targetId, content, startedAt: new Date().toISOString() });
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
    if (attachment.status === "ready") return "已就绪";
    if (attachment.status === "failed") return "读取失败";
    if (attachment.status === "deleting") return "正在清理";
    return "正在读取";
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
  // 展示主回答、子任务状态及失败重试，子任务详情按需授权加载。
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
        {message.role === "assistant" && conversation && <AgentTasks tasks={message.agent_tasks || []} conversationId={conversation.id} answerId={message.id} />}
        {message.role === "assistant"
          ? <AnswerContent message={message} />
          : <div className="message-content">
              <span>{message.content}</span>
              {!!message.attachments.length && renderMessageAttachments(message.attachments)}
            </div>}
        {message.status === "generating" && <AgentProgress progress={message.progress} startedAt={message.created_at} />}
        {message.status === "failed" && (
          <div className="message-error" role="alert">这次回答没有完成 · {message.error || "可以再试一次或检查模型设置。"}</div>
        )}
        {message.status === "stopped" && <div className="message-progress">已停止 · 已保留这部分回答</div>}
        {(message.status === "failed" || message.status === "stopped") && conversation?.messages.at(-1)?.id === message.id && (
          <button className="retry-button" data-action="retry" data-message={message.id} onClick={changeRun} disabled={changingRun || !!pending}>
            再试一次
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
        onCancel={returnToChat}
      />
    );
  }
  return (
    <div className="app-shell">
      {mobile && navigationOpen && <button className="navigation-backdrop" aria-label="关闭会话列表" tabIndex={-1} onClick={closeNavigation} />}
      <aside
        id="conversation-navigation"
        ref={navigation}
        className={`sidebar ${navigationOpen ? "is-open" : ""}`}
        inert={mobile && !navigationOpen}
        role={mobile ? "dialog" : undefined}
        aria-modal={mobile && navigationOpen ? true : undefined}
        aria-label="会话导航"
      >
        <button className="navigation-close" type="button" aria-label="收起会话列表" onClick={closeNavigation}>×</button>
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            ✳
          </span>
          <div>
            <strong>对话</strong>
            <span>一处会记住你的对话空间</span>
          </div>
        </div>
        <button
          className="new-conversation"
          onClick={newConversation}
          disabled={creating || loading}
        >
          <span aria-hidden="true">＋</span>开始新对话
        </button>
        <div className="sidebar-tools" aria-label="工具">
          <button className="sidebar-tool" onClick={openKnowledge} aria-pressed={screen === 'knowledge'}>
            <span aria-hidden="true">▤</span>知识库
          </button>
          {screen === 'knowledge' && (
            <button className="sidebar-tool" onClick={returnToChat}>
              <span aria-hidden="true">←</span>返回对话
            </button>
          )}
        </div>
        <div className="section-label">
          你的会话 <span>{conversations.length || ""}</span>
        </div>
        <nav className="conversation-list" aria-label="会话列表">
          {conversations.map(renderConversation)}
          {!conversations.length && (
            <p className="sidebar-empty">还没有对话，开始一个吧。</p>
          )}
        </nav>
        <div className="sidebar-bottom">
          <details className="account-menu">
            <summary>
              <span className="user-name">{user.username}</span>
              <span aria-hidden="true">账号</span>
            </summary>
            <div className="account-menu-actions">
              <button onClick={openPasswordScreen}>修改密码</button>
              <button onClick={logout}>退出登录</button>
            </div>
          </details>
          <div className="local-note">
            <span className="status-dot" />
            内容保存在你的账号中
          </div>
        </div>
      </aside>
      <main className="main-panel" inert={mobile && navigationOpen}>
        <header className="topbar">
          <div>
            <button ref={navigationTrigger} className="navigation-toggle" type="button" aria-label="打开会话列表" aria-expanded={navigationOpen} aria-controls="conversation-navigation" onClick={openNavigation}>☰</button>
            <h1>个人对话空间</h1>
            <span className="topbar-divider">/</span>
            <span className="topbar-subtitle">{screen === "knowledge" ? "长期资料" : conversation?.title || "日常、灵感与思考"}</span>
          </div>
          <div className="topbar-actions">
            {screen === "chat" && conversation && (
              <details className="conversation-menu">
                <summary aria-label="对话选项">⋯</summary>
                <div>
                  <button type="button" onClick={deleteConversationFromMenu}>删除当前对话</button>
                </div>
              </details>
            )}
            <span className="user-pill" title={user.username}>{user.username}</span>
          </div>
        </header>
        {error && (
          <div className="global-error" role="alert">
            {error}
          </div>
        )}
        {screen === 'knowledge' ? <KnowledgePanel /> : <>
        <SourceDeletionPending revision={sourceRevision} onChanged={refreshSources} />
        {deleteTarget && <SourceDeletion key={deleteTarget.path} target={deleteTarget} onClose={closeDeletion} onChanged={refreshSources} />}
        <div className="chat-layout">
            <div className="chat-scroll" ref={chatScroll} onScroll={trackReadingPosition}>
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
                    <div className="eyebrow">开始一段对话</div>
                    <h2>把还没想清楚的事，放在这里。</h2>
                    <p>一句话、一份资料，或一个还没成形的念头。先说出来，再慢慢理清。</p>
                    <div className="topic-tags" aria-label="可以这样开始">
                      <button type="button" data-prompt="今天最值得先做什么？" onClick={useExamplePrompt}>今天最值得先做什么？</button>
                      <button type="button" data-prompt="帮我整理一个还没想清楚的想法。" onClick={useExamplePrompt}>整理一个想法</button>
                      <button type="button" data-prompt="请帮我读一份资料，并总结重点。" onClick={useExamplePrompt}>读一份资料</button>
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
                        <AgentProgress
                          progress={{ code: "preparing", label: "正在准备回答…" }}
                          startedAt={currentPending.startedAt}
                        />
                      </>
                    )}
                  </div>
                )}
              </div>
            </div>
            {showLatest && <button type="button" className="return-to-latest" onClick={returnToLatest}>↓ 回到最新</button>}
            <div className="composer-wrap">
              {streamNotice && <p className="stream-notice" role="status">{streamNotice}</p>}
              <form className="composer" ref={composer} onSubmit={send}>
                {conversation && <KnowledgeSelector key={conversation.id} conversationId={conversation.id} disabled={!!pending || !!generating || changingRun} />}
                <AttachmentPanel revision={sourceRevision} resetToken={attachmentReset} onDelete={deleteAttachment} conversationId={activeId.current} disabled={!!pending || !!generating || changingRun || creating || loading} />
                <textarea
                  ref={draftInput}
                  aria-label="消息"
                  placeholder="写下你想聊的事…"
                  value={draft}
                  onChange={editDraft}
                  onKeyDown={composeKey}
                  onCompositionStart={startComposition}
                  onCompositionEnd={finishComposition}
                  maxLength={32000}
                  disabled={loading || creating}
                  rows={mobile ? 1 : 2}
                />
                <div className="composer-bottom" onPointerDown={preserveComposerFocus}>
                  <span>
                    <span className="desktop-composer-hint">Enter 发送 <i>·</i> Shift + Enter 换行</span>
                    <span className="mobile-composer-hint">点击发送</span>
                  </span>
                  {activeRun ? (
                    <button type="button" className="stop-button" data-action="stop" data-message={activeRun.id} onClick={changeRun} disabled={changingRun}>
                      停止回答
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
              <p className="composer-note">回答可能不准确，重要信息请核实。</p>
            </div>
        </div>
        </>}
      </main>
    </div>
  );
}
