import { useEffect, useRef, useState, type ChangeEvent, type FormEvent, type MouseEvent } from "react";

import { AnswerContent } from "./AnswerContent";
import { api, errorMessage, type Conversation, type ConversationDetail, type Message } from "./api";

type AdminUser = {
  id: string;
  username: string;
  status: "active" | "disabled";
  must_change_password: boolean;
  created_at: string;
  last_login_at: string | null;
};
type AdminConversation = Conversation & { matched_message_id: string | null };
type AdminMemory = { id: string; fact?: string; status: string };
type AdminSearch = { id: string; query: string; status: string; created_at: string };
type AdminUsage = { id: string; model: string; purpose: string; total_tokens: number | null; credits: string | null; created_at: string };
type AuditRow = { id: string; admin_user_id: string; target_user_id: string | null; resource_type: string; resource_id: string | null; action: string; ip_address: string | null; created_at: string };
type DetailTab = "conversations" | "memories" | "search" | "usage";

/** 把服务端时间转换为简短本地时间，输入时间字符串并输出用于列表的文本。 */
function displayTime(value: string | null) {
  return value ? new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "—";
}

/** 根据附件名称判断管理端是否提供图片或 PDF 的内联预览。 */
function previewKind(filename: string): "image" | "pdf" | null {
  if (/\.(png|jpe?g|gif|webp)$/i.test(filename)) return "image";
  if (/\.pdf$/i.test(filename)) return "pdf";
  return null;
}

/** 展示普通用户管理、跨账号内容和审计，输入为空并通过受保护 API 读取数据。 */
export function AdminPanel() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [auditUsers, setAuditUsers] = useState<AdminUser[]>([]);
  const [selected, setSelected] = useState<AdminUser | null>(null);
  const [section, setSection] = useState<"users" | "audit">("users");
  const [detailTab, setDetailTab] = useState<DetailTab>("conversations");
  const [conversations, setConversations] = useState<AdminConversation[]>([]);
  const [conversation, setConversation] = useState<ConversationDetail | null>(null);
  const [memories, setMemories] = useState<AdminMemory[]>([]);
  const [searches, setSearches] = useState<AdminSearch[]>([]);
  const [usage, setUsage] = useState<AdminUsage[]>([]);
  const [audit, setAudit] = useState<AuditRow[]>([]);
  const [accountQuery, setAccountQuery] = useState("");
  const [accountStatus, setAccountStatus] = useState("");
  const [conversationQuery, setConversationQuery] = useState("");
  const [conversationStart, setConversationStart] = useState("");
  const [conversationEnd, setConversationEnd] = useState("");
  const [auditTarget, setAuditTarget] = useState("");
  const [auditAction, setAuditAction] = useState("");
  const [auditStart, setAuditStart] = useState("");
  const [auditEnd, setAuditEnd] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [passwordTarget, setPasswordTarget] = useState<AdminUser | null>(null);
  const [temporaryPassword, setTemporaryPassword] = useState("");
  const [preview, setPreview] = useState<{ filename: string; url: string; kind: "image" | "pdf" } | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [conversationOffset, setConversationOffset] = useState(0);
  const [auditOffset, setAuditOffset] = useState(0);
  const [moreConversations, setMoreConversations] = useState(false);
  const [moreAudit, setMoreAudit] = useState(false);
  const messagePane = useRef<HTMLDivElement>(null);
  const viewEpoch = useRef(0);
  const conversationListEpoch = useRef(0);
  const conversationEpoch = useRef(0);
  const auditEpoch = useRef(0);
  const accountListEpoch = useRef(0);

  /** 首次挂载时读取普通用户列表，避免预先读取他人的内容。 */
  useEffect(function loadInitialAccounts() { void loadUsers(); }, []);

  /** 按当前用户名和状态过滤普通用户账号，失败时展示可见错误。 */
  async function loadUsers() {
    const request = ++accountListEpoch.current;
    try {
      const params = new URLSearchParams();
      if (accountQuery.trim()) params.set("query", accountQuery.trim());
      if (accountStatus) params.set("status", accountStatus);
      const rows = await api<AdminUser[]>(`/admin/users?${params}`);
      if (request === accountListEpoch.current) setUsers(rows);
    } catch (failure) { if (request === accountListEpoch.current) setError(errorMessage(failure)); }
  }

  /** 创建普通用户账号，输入表单事件并在成功后刷新账号列表。 */
  async function createUser(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setLoading(true);
    try {
      await api("/admin/users", { method: "POST", body: JSON.stringify({ username, password }) });
      setUsername("");
      setPassword("");
      setCreateOpen(false);
      setNotice("账号已创建。");
      await loadUsers();
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setLoading(false); }
  }

  /** 停用或启用目标账号，停用前确认登录会立即失效。 */
  async function toggleUser(user: AdminUser) {
    if (user.status === "active" && !window.confirm(`停用 ${user.username} 后，其登录会立即失效。继续吗？`)) return;
    try {
      await api(`/admin/users/${user.id}`, { method: "PATCH", body: JSON.stringify({ status: user.status === "active" ? "disabled" : "active" }) });
      setNotice(user.status === "active" ? "账号已停用。" : "账号已启用。");
      setError("");
      await loadUsers();
    } catch (failure) { setError(errorMessage(failure)); }
  }

  /** 为目标普通用户生成新临时密码，响应只在当前弹层保留一次。 */
  async function resetPassword() {
    if (!passwordTarget) return;
    setLoading(true);
    setError("");
    try {
      const result = await api<{ temporary_password: string }>(`/admin/users/${passwordTarget.id}/reset-password`, { method: "POST" });
      setTemporaryPassword(result.temporary_password);
      await loadUsers();
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setLoading(false); }
  }

  /** 关闭临时密码弹层并清除唯一一次显示的明文。 */
  function closePassword() {
    setPasswordTarget(null);
    setTemporaryPassword("");
  }

  /** 读取目标账号的会话页，输入账号和页码并更新当前列表状态。 */
  async function loadConversations(user: AdminUser, offset = 0) {
    const view = viewEpoch.current;
    const request = ++conversationListEpoch.current;
    const params = new URLSearchParams({ offset: String(offset) });
    if (conversationQuery.trim()) params.set("query", conversationQuery.trim());
    if (conversationStart) params.set("start", conversationStart);
    if (conversationEnd) params.set("end", conversationEnd);
    const rows = await api<AdminConversation[]>(`/admin/users/${user.id}/conversations?${params}`);
    // 只允许当前账号、当前栏目和最新一次查询更新阅读列表。
    if (view !== viewEpoch.current || request !== conversationListEpoch.current) return;
    setConversations(offset ? [...conversations, ...rows] : rows);
    setConversationOffset(offset);
    setMoreConversations(rows.length === 100);
  }

  /** 从账号列表进入用户详情，先只读取会话列表。 */
  async function selectUser(user: AdminUser) {
    const view = ++viewEpoch.current;
    conversationEpoch.current += 1;
    conversationListEpoch.current += 1;
    setSelected(user);
    setConversation(null);
    setConversations([]);
    setMemories([]);
    setSearches([]);
    setUsage([]);
    setDetailTab("conversations");
    setError("");
    setNotice("");
    setConversationQuery("");
    setConversationStart("");
    setConversationEnd("");
    try {
      const rows = await api<AdminConversation[]>(`/admin/users/${user.id}/conversations`);
      if (view !== viewEpoch.current) return;
      setConversations(rows);
      setConversationOffset(0);
      setMoreConversations(rows.length === 100);
    } catch (failure) { if (view === viewEpoch.current) setError(errorMessage(failure)); }
  }

  /** 打开指定栏目时才读取其内容，避免查看账号时并行访问所有敏感数据。 */
  async function selectTab(tab: DetailTab) {
    if (!selected) return;
    const view = ++viewEpoch.current;
    conversationEpoch.current += 1;
    setDetailTab(tab);
    setConversation(null);
    if (tab === "memories") setMemories([]);
    if (tab === "search") setSearches([]);
    if (tab === "usage") setUsage([]);
    setError("");
    try {
      if (tab === "conversations") await loadConversations(selected);
      if (tab === "memories") {
        const rows = await api<AdminMemory[]>(`/admin/users/${selected.id}/memories`);
        if (view === viewEpoch.current) setMemories(rows);
      }
      if (tab === "search") {
        const rows = await api<AdminSearch[]>(`/admin/users/${selected.id}/search`);
        if (view === viewEpoch.current) setSearches(rows);
      }
      if (tab === "usage") {
        const rows = await api<AdminUsage[]>(`/admin/users/${selected.id}/usage`);
        if (view === viewEpoch.current) setUsage(rows);
      }
    } catch (failure) { if (view === viewEpoch.current) setError(errorMessage(failure)); }
  }

  /** 按搜索条件重新查找当前账号会话，并清除上一个会话详情。 */
  async function findConversations(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    const view = viewEpoch.current;
    const request = conversationListEpoch.current + 1;
    setConversation(null);
    try { await loadConversations(selected); }
    catch (failure) { if (view === viewEpoch.current && request === conversationListEpoch.current) setError(errorMessage(failure)); }
  }

  /** 读取会话详情，若列表命中消息正文则定位到对应消息。 */
  async function selectConversation(item: AdminConversation) {
    if (!selected) return;
    const view = viewEpoch.current;
    const request = ++conversationEpoch.current;
    setConversation(null);
    setError("");
    try {
      const detail = await api<ConversationDetail>(`/admin/users/${selected.id}/conversations/${item.id}`);
      if (view !== viewEpoch.current || request !== conversationEpoch.current) return;
      setConversation(detail);
      /** 在会话正文完成绘制后定位命中的消息，未命中时滚动到顶部。 */
      requestAnimationFrame(function locateMatchedMessage() {
        if (view !== viewEpoch.current || request !== conversationEpoch.current) return;
        if (item.matched_message_id) document.getElementById(`admin-message-${item.matched_message_id}`)?.scrollIntoView({ block: "center" });
        else messagePane.current?.scrollTo({ top: 0 });
      });
    } catch (failure) { if (view === viewEpoch.current && request === conversationEpoch.current) setError(errorMessage(failure)); }
  }

  /** 按筛选条件加载一页只读审计，输入偏移并更新审计列表。 */
  async function loadAudit(offset = 0) {
    const view = viewEpoch.current;
    const request = ++auditEpoch.current;
    const params = new URLSearchParams({ offset: String(offset) });
    if (auditTarget) params.set("target_user_id", auditTarget);
    if (auditAction) params.set("action", auditAction);
    if (auditStart) params.set("start", auditStart);
    if (auditEnd) params.set("end", auditEnd);
    const rows = await api<AuditRow[]>(`/admin/audit?${params}`);
    if (view !== viewEpoch.current || request !== auditEpoch.current) return;
    setAudit(offset ? [...audit, ...rows] : rows);
    setAuditOffset(offset);
    setMoreAudit(rows.length === 100);
  }

  /** 打开审计页面并读取首批记录。 */
  async function showAudit() {
    const view = ++viewEpoch.current;
    conversationEpoch.current += 1;
    setSection("audit");
    setSelected(null);
    setConversation(null);
    setAudit([]);
    setError("");
    try {
      const targets = await api<AdminUser[]>("/admin/users");
      if (view !== viewEpoch.current) return;
      setAuditUsers(targets);
      await loadAudit();
    }
    catch (failure) { if (view === viewEpoch.current) setError(errorMessage(failure)); }
  }

  /** 返回普通用户账号列表并使之前的跨账号内容请求失效。 */
  function showUsers() {
    viewEpoch.current += 1;
    conversationEpoch.current += 1;
    setSection("users");
    setSelected(null);
    setConversation(null);
    setError("");
  }

  /** 提交审计筛选条件并从第一页重新读取。 */
  async function findAudit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const view = viewEpoch.current;
    const request = auditEpoch.current + 1;
    try { await loadAudit(); }
    catch (failure) { if (view === viewEpoch.current && request === auditEpoch.current) setError(errorMessage(failure)); }
  }

  /** 将当前消息正文复制到剪贴板并反馈操作结果。 */
  async function copyMessage(content: string) {
    try { await navigator.clipboard.writeText(content); setNotice("消息已复制。"); }
    catch { setError("复制失败，请手动选择文字。"); }
  }

  /** 根据目标附件和会话构造受保护的下载或预览地址。 */
  function attachmentUrl(attachmentId: string, mode: "file" | "preview") {
    return `/api/admin/users/${selected?.id}/conversations/${conversation?.id}/attachments/${attachmentId}/${mode}`;
  }

  /** 提交账号搜索表单并读取匹配的普通用户列表。 */
  function findUsers(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void loadUsers();
  }

  /** 切换创建账号表单的展开状态。 */
  function toggleCreateForm() {
    setCreateOpen(!createOpen);
  }

  /** 为选中的普通用户打开一次性密码重置弹层。 */
  function openPassword(user: AdminUser) {
    setPasswordTarget(user);
    setTemporaryPassword("");
  }

  /** 在窄屏返回当前账号的会话列表并撤销未完成的详情请求。 */
  function backToConversations() {
    conversationEpoch.current += 1;
    setConversation(null);
  }

  /** 关闭附件预览层并释放受保护文件地址。 */
  function closePreview() {
    setPreview(null);
  }

  /** 根据普通用户资料生成账号卡片，输出数据入口与账号维护操作。 */
  function renderAccount(user: AdminUser) {
    /** 打开本卡片对应账号的数据页面。 */
    function openData() { void selectUser(user); }
    /** 打开本卡片对应账号的临时密码弹层。 */
    function openReset() { openPassword(user); }
    /** 切换本卡片对应账号的有效状态。 */
    function changeStatus() { void toggleUser(user); }
    return <article className="admin-account-card" key={user.id}>
      <div><strong>{user.username}</strong><span className={`account-state ${user.status}`}>{user.status === "active" ? "有效" : "已停用"}</span></div>
      <small>最近登录：{displayTime(user.last_login_at)}{user.must_change_password ? " · 待改密" : ""}</small>
      <div className="admin-card-actions"><button type="button" onClick={openData}>查看数据</button><button type="button" onClick={openReset}>重置密码</button><button type="button" onClick={changeStatus}>{user.status === "active" ? "停用" : "启用"}</button></div>
    </article>;
  }

  /** 根据会话摘要生成列表项，输入会话并输出可打开的标题和时间。 */
  function renderConversation(item: AdminConversation) {
    /** 打开本列表项对应的会话详情。 */
    function openConversation() { void selectConversation(item); }
    return <button key={item.id} type="button" className={conversation?.id === item.id ? "active" : ""} onClick={openConversation}><strong>{item.title}</strong><small>{displayTime(item.created_at)}</small></button>;
  }

  /** 将单条消息排为可阅读内容，输入消息并输出角色、正文、附件和状态。 */
  function renderMessage(message: Message) {
    /** 生成当前消息附件的预览和下载操作。 */
    function renderAttachment(attachment: Message["attachments"][number]) {
      const kind = previewKind(attachment.filename);
      /** 仅为受支持格式打开当前附件的预览层。 */
      function openAttachmentPreview() {
        if (kind) setPreview({ filename: attachment.filename, url: attachmentUrl(attachment.id, "preview"), kind });
      }
      return <div className="admin-reader-attachment" key={attachment.id}><span>{attachment.filename}</span>{kind && <button type="button" onClick={openAttachmentPreview}>预览</button>}<a href={attachmentUrl(attachment.id, "file")}>下载</a></div>;
    }
    /** 复制当前用户消息正文。 */
    function copyCurrentMessage() { void copyMessage(message.content); }
    return <article id={`admin-message-${message.id}`} className={`message ${message.role} admin-reader-message`} key={message.id}>
      <div className="message-label">{message.role === "user" ? selected?.username : "助手"}<time>{displayTime(message.created_at)}</time>{message.status !== "complete" && <span>{message.status === "failed" ? "失败" : message.status === "stopped" ? "已停止" : "生成中"}</span>}</div>
      {message.role === "assistant" ? <AnswerContent message={message} resourceRoot={`/api/admin/users/${selected?.id}`} /> : <div className="message-content"><span>{message.content}</span>{message.attachments?.map(renderAttachment)}</div>}
      {message.role === "user" && !!message.content && <button className="admin-copy" type="button" onClick={copyCurrentMessage}>复制</button>}
    </article>;
  }

  /** 打开审计栏目，输入点击事件并启动审计查询。 */
  function openAudit() { void showAudit(); }
  /** 记录普通用户搜索关键字，输入文本变更事件并更新筛选条件。 */
  function changeAccountQuery(event: ChangeEvent<HTMLInputElement>) { setAccountQuery(event.target.value); }
  /** 记录普通用户状态筛选，输入选择事件并更新筛选条件。 */
  function changeAccountStatus(event: ChangeEvent<HTMLSelectElement>) { setAccountStatus(event.target.value); }
  /** 记录新账号名称，输入文本变更事件并更新表单。 */
  function changeUsername(event: ChangeEvent<HTMLInputElement>) { setUsername(event.target.value); }
  /** 记录新账号密码，输入文本变更事件并更新表单。 */
  function changePassword(event: ChangeEvent<HTMLInputElement>) { setPassword(event.target.value); }
  /** 记录会话搜索词，输入文本变更事件并更新筛选条件。 */
  function changeConversationQuery(event: ChangeEvent<HTMLInputElement>) { setConversationQuery(event.target.value); }
  /** 记录会话开始日期，输入日期变更事件并更新筛选条件。 */
  function changeConversationStart(event: ChangeEvent<HTMLInputElement>) { setConversationStart(event.target.value); }
  /** 记录会话结束日期，输入日期变更事件并更新筛选条件。 */
  function changeConversationEnd(event: ChangeEvent<HTMLInputElement>) { setConversationEnd(event.target.value); }
  /** 读取下一页会话，输入为空并保留已显示的会话。 */
  function loadMoreConversations() { if (selected) void loadConversations(selected, conversationOffset + 100); }
  /** 记录审计目标账号，输入选择事件并更新筛选条件。 */
  function changeAuditTarget(event: ChangeEvent<HTMLSelectElement>) { setAuditTarget(event.target.value); }
  /** 记录审计操作类型，输入选择事件并更新筛选条件。 */
  function changeAuditAction(event: ChangeEvent<HTMLSelectElement>) { setAuditAction(event.target.value); }
  /** 记录审计开始日期，输入日期变更事件并更新筛选条件。 */
  function changeAuditStart(event: ChangeEvent<HTMLInputElement>) { setAuditStart(event.target.value); }
  /** 记录审计结束日期，输入日期变更事件并更新筛选条件。 */
  function changeAuditEnd(event: ChangeEvent<HTMLInputElement>) { setAuditEnd(event.target.value); }
  /** 读取下一页审计记录，输入为空并保留已显示的记录。 */
  function loadMoreAudit() { void loadAudit(auditOffset + 100); }
  /** 复制弹层中的一次性密码，输入为空并显示复制反馈。 */
  function copyTemporaryPassword() { void copyMessage(temporaryPassword); }
  /** 确认生成临时密码，输入为空并发起密码重置。 */
  function generateTemporaryPassword() { void resetPassword(); }
  /** 阻止预览内容上的点击关闭外层弹层。 */
  function keepPreviewOpen(event: MouseEvent<HTMLElement>) { event.stopPropagation(); }
  /** 根据详情栏目元组生成切换按钮，输出可点击的栏目标签。 */
  function renderDetailTab([key, label]: readonly [DetailTab, string]) {
    /** 切换到当前按钮对应的详情栏目。 */
    function openDetailTab() { void selectTab(key); }
    return <button type="button" key={key} className={detailTab === key ? "active" : ""} onClick={openDetailTab}>{label}</button>;
  }
  /** 根据记忆记录生成只读列表行，输出正文和状态。 */
  function renderMemory(row: AdminMemory) { return <div className="admin-row" key={row.id}><span>{row.fact || "（无正文）"}</span><small>{row.status}</small></div>; }
  /** 根据搜索记录生成只读列表行，输出查询词、时间和状态。 */
  function renderSearch(row: AdminSearch) { return <div className="admin-row" key={row.id}><span>{row.query}</span><small>{displayTime(row.created_at)} · {row.status}</small></div>; }
  /** 根据用量记录生成只读列表行，输出用途、模型、用量和时间。 */
  function renderUsage(row: AdminUsage) { return <div className="admin-row" key={row.id}><span>{row.purpose} · {row.model}</span><small>{row.total_tokens ?? row.credits ?? "—"} · {displayTime(row.created_at)}</small></div>; }
  /** 根据用户资料生成审计目标选项，输出账号标识与名称。 */
  function renderAuditTarget(user: AdminUser) { return <option key={user.id} value={user.id}>{user.username}</option>; }
  /** 根据审计记录生成只读列表行，输出目标、资源、操作和时间。 */
  function renderAuditRow(row: AuditRow) {
    /** 判断审计目标是否为当前候选账号，输出匹配结果。 */
    function isTarget(user: AdminUser) { return user.id === row.target_user_id; }
    return <div className="admin-row admin-audit-row" key={row.id}><span>admin · {auditUsers.find(isTarget)?.username || "系统"} · {row.resource_type} · {row.action}{row.resource_id && <small> · {row.resource_id}</small>}</span><small>{displayTime(row.created_at)} · {row.ip_address || "本机"}</small></div>;
  }

  return <div className="page-scroll admin-page admin-workspace">
    <header className="admin-workspace-heading">
      <div><h2>{section === "audit" ? "访问审计" : selected ? selected.username : "用户账号"}</h2>
        {selected && <button className="admin-text-button" type="button" onClick={showUsers}>返回账号列表</button>}</div>
      <nav aria-label="数据管理"><button type="button" className={section === "users" ? "active" : ""} onClick={showUsers}>用户</button><button type="button" className={section === "audit" ? "active" : ""} onClick={openAudit}>审计</button></nav>
    </header>
    {error && <div className="global-error" role="alert">{error}</div>}
    {notice && <div className="admin-notice" role="status">{notice}</div>}

    {section === "users" && !selected && <>
      <div className="admin-toolbar"><form onSubmit={findUsers}>
        <input aria-label="搜索用户名" placeholder="搜索用户名" value={accountQuery} onChange={changeAccountQuery} />
        <select aria-label="账号状态" value={accountStatus} onChange={changeAccountStatus}><option value="">全部状态</option><option value="active">有效</option><option value="disabled">已停用</option></select>
        <button type="submit">查找</button>
      </form><button className="admin-primary" type="button" onClick={toggleCreateForm}>创建用户</button></div>
      {createOpen && <form className="admin-card admin-create-form" onSubmit={createUser}>
        <label>用户名<input value={username} onChange={changeUsername} required /></label>
        <label>临时密码<input type="password" value={password} onChange={changePassword} minLength={10} required /></label>
        <button className="admin-primary" type="submit" disabled={loading}>创建账号</button>
      </form>}
      <section className="admin-account-grid" aria-label="普通用户账号">
        {users.map(renderAccount)}
        {!users.length && <p className="admin-empty">没有符合条件的账号。</p>}
      </section>
    </>}

    {section === "users" && selected && <>
      <nav className="admin-detail-tabs" aria-label="用户数据类型">{([["conversations", "对话"], ["memories", "长期记忆"], ["search", "搜索记录"], ["usage", "调用用量"]] as const).map(renderDetailTab)}</nav>
      {detailTab === "conversations" && <>
        <form className="admin-conversation-search" onSubmit={findConversations}><input aria-label="搜索会话和消息" placeholder="搜索会话和消息" value={conversationQuery} onChange={changeConversationQuery} /><label>从<input type="date" value={conversationStart} onChange={changeConversationStart} /></label><label>到<input type="date" value={conversationEnd} onChange={changeConversationEnd} /></label><button type="submit">查找</button></form>
        <div className={`admin-reader ${conversation ? "admin-reader-open" : ""}`}>
          <aside className="admin-reader-list" aria-label="会话列表">{conversations.map(renderConversation)}{!conversations.length && <p className="admin-empty">没有会话。</p>}{moreConversations && <button type="button" onClick={loadMoreConversations}>加载更多</button>}</aside>
          <section className="admin-reader-content" ref={messagePane} aria-label="会话内容">{conversation ? <>
            <header><button className="admin-reader-back" type="button" onClick={backToConversations}>返回会话</button><div><h3>{conversation.title}</h3><small>{displayTime(conversation.created_at)}</small></div></header>
            <div className="admin-reader-messages">{conversation.messages.map(renderMessage)}</div>
          </> : <div className="admin-reader-placeholder">选择一段会话</div>}</section>
        </div>
      </>}
      {detailTab === "memories" && <div className="admin-activity-list">{memories.map(renderMemory)}{!memories.length && <p className="admin-empty">暂无长期记忆。</p>}</div>}
      {detailTab === "search" && <div className="admin-activity-list">{searches.map(renderSearch)}{!searches.length && <p className="admin-empty">暂无搜索记录。</p>}</div>}
      {detailTab === "usage" && <div className="admin-activity-list">{usage.map(renderUsage)}{!usage.length && <p className="admin-empty">暂无调用记录。</p>}</div>}
    </>}

    {section === "audit" && <><form className="admin-toolbar admin-audit-filters" onSubmit={findAudit}><select aria-label="目标账号" value={auditTarget} onChange={changeAuditTarget}><option value="">全部账号</option>{auditUsers.map(renderAuditTarget)}</select><select aria-label="操作" value={auditAction} onChange={changeAuditAction}><option value="">全部操作</option><option value="view">查看</option><option value="download">下载</option><option value="preview">预览</option><option value="recover_password">恢复密码</option></select><label>从<input type="date" value={auditStart} onChange={changeAuditStart} /></label><label>到<input type="date" value={auditEnd} onChange={changeAuditEnd} /></label><button type="submit">查找</button></form>
      <div className="admin-activity-list">{audit.map(renderAuditRow)}{!audit.length && <p className="admin-empty">暂无审计记录。</p>}{moreAudit && <button type="button" onClick={loadMoreAudit}>加载更多</button>}</div></>}

    {passwordTarget && <div className="admin-dialog-backdrop"><section className="admin-dialog" role="dialog" aria-modal="true" aria-labelledby="admin-reset-title"><h3 id="admin-reset-title">重置 {passwordTarget.username} 的密码</h3>{temporaryPassword ? <><p>请复制并安全交给用户。关闭后无法再次查看。</p><output className="admin-temporary-password">{temporaryPassword}</output><div className="admin-dialog-actions"><button type="button" onClick={copyTemporaryPassword}>复制密码</button><button className="admin-primary" type="button" onClick={closePassword}>完成</button></div></> : <><p>重置后，该用户的现有登录会失效，下次登录必须修改密码。</p><div className="admin-dialog-actions"><button type="button" onClick={closePassword}>取消</button><button className="admin-primary" type="button" disabled={loading} onClick={generateTemporaryPassword}>生成临时密码</button></div></>}</section></div>}
    {preview && <div className="admin-dialog-backdrop" onClick={closePreview}><section className="admin-preview" role="dialog" aria-modal="true" aria-label={preview.filename} onClick={keepPreviewOpen}><header><strong>{preview.filename}</strong><button type="button" onClick={closePreview}>关闭</button></header>{preview.kind === "image" ? <img src={preview.url} alt={preview.filename} /> : <iframe title={preview.filename} src={preview.url} sandbox="allow-same-origin" />}</section></div>}
  </div>;
}
