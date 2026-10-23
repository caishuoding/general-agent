import { useEffect, useState, type ChangeEvent, type FormEvent } from "react";

import { api, errorMessage } from "./api";

type AdminUser = {
  id: string;
  username: string;
  role: "admin" | "user";
  status: "active" | "disabled";
  must_change_password: boolean;
  created_at: string;
  last_login_at: string | null;
};

type AdminConversation = {
  id: string;
  title: string;
  created_at: string;
};

type AdminAttachment = {
  id: string;
  filename: string;
  status: string;
};

type AdminMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: string;
  created_at: string;
  attachments: AdminAttachment[];
};

type AdminConversationDetail = AdminConversation & { messages: AdminMessage[] };

type AdminMemory = {
  id: string;
  fact?: string;
  status: string;
  conversation_title?: string | null;
};

type AdminSearch = {
  id: string;
  query: string;
  status: string;
  error: string | null;
  created_at: string;
};

type AdminUsage = {
  id: string;
  model: string;
  purpose: string;
  status: string;
  unit: string;
  credits: string | null;
  total_tokens: number | null;
  created_at: string;
};

type DetailTab = "conversations" | "memories" | "search" | "usage";

type AdminPanelProps = {
  /** 当前登录管理员的账号标识，用于区分“修改自己密码”和“重置他人密码”。 */
  currentUserId: string;
};

// 展示管理员账号管理和跨账号只读内容，所有操作都通过后端权限校验。
export function AdminPanel({ currentUserId }: AdminPanelProps) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [selected, setSelected] = useState<AdminUser | null>(null);
  const [conversations, setConversations] = useState<AdminConversation[]>([]);
  const [conversation, setConversation] = useState<AdminConversationDetail | null>(null);
  const [memories, setMemories] = useState<AdminMemory[]>([]);
  const [searches, setSearches] = useState<AdminSearch[]>([]);
  const [usage, setUsage] = useState<AdminUsage[]>([]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"user" | "admin">("user");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [detailTab, setDetailTab] = useState<DetailTab>("conversations");
  const [passwordTarget, setPasswordTarget] = useState<AdminUser | null>(null);
  const [temporaryPassword, setTemporaryPassword] = useState("");
  const [notice, setNotice] = useState("");

  // 首次进入后台时读取全部账号。
  useEffect(
    function loadUsers() {
      void refreshUsers();
    },
    [],
  );

  // 重新读取账号列表，并在失败时显示统一错误。
  async function refreshUsers() {
    try {
      setUsers(await api<AdminUser[]>("/admin/users"));
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }

  /** 展开或收起创建账号表单，避免长期占据页面首屏。 */
  function toggleCreateForm() {
    setCreateOpen((value) => !value);
  }

  // 创建普通用户或管理员，成功后重新加载账号列表。
  async function createUser(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      await api<AdminUser>("/admin/users", {
        method: "POST",
        body: JSON.stringify({ username, password, role }),
      });
      setUsername("");
      setPassword("");
      setRole("user");
      await refreshUsers();
      setCreateOpen(false);
      setNotice("账号已创建。请把临时密码安全地交给用户。");
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }

  // 停用前确认风险，或直接重新启用账号，并刷新列表。
  async function toggleUser(user: AdminUser) {
    if (user.status === "active" && !window.confirm(`停用 ${user.username} 后，该账号将无法登录。继续吗？`)) return;
    setError("");
    setNotice("");
    try {
      await api<AdminUser>(`/admin/users/${user.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          status: user.status === "active" ? "disabled" : "active",
        }),
      });
      await refreshUsers();
      setNotice(user.status === "active" ? "账号已停用。" : "账号已重新启用。");
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }

  /** 打开临时密码设置层，避免使用浏览器原生输入框。 */
  function startPasswordReset(user: AdminUser) {
    setPasswordTarget(user);
    setTemporaryPassword("");
    setError("");
    setNotice("");
  }

  /** 关闭临时密码设置层，不修改目标账号。 */
  function closePasswordReset() {
    setPasswordTarget(null);
    setTemporaryPassword("");
  }

  /** 更新管理员输入的临时密码草稿。 */
  function changeTemporaryPassword(event: ChangeEvent<HTMLInputElement>) {
    setTemporaryPassword(event.target.value);
  }

  /** 保存目标账号的临时密码，成功后关闭设置层并提示管理员转交。 */
  async function submitPasswordReset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!passwordTarget || !temporaryPassword) return;
    setError("");
    try {
      await api<AdminUser>(`/admin/users/${passwordTarget.id}/reset-password`, {
        method: "POST",
        body: JSON.stringify({ password: temporaryPassword }),
      });
      await refreshUsers();
      setPasswordTarget(null);
      setTemporaryPassword("");
      setNotice("临时密码已设置。用户下次登录时需要修改密码。");
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }

  // 读取目标账号的只读内容；切换账号时清空旧详情。
  async function selectUser(user: AdminUser) {
    setSelected(user);
    setConversation(null);
    setDetailTab("conversations");
    setError("");
    setNotice("");
    try {
      const [items, memoryItems, searchItems, usageItems] = await Promise.all([
        api<AdminConversation[]>(`/admin/users/${user.id}/conversations`),
        api<AdminMemory[]>(`/admin/users/${user.id}/memories`),
        api<AdminSearch[]>(`/admin/users/${user.id}/search`),
        api<AdminUsage[]>(`/admin/users/${user.id}/usage`),
      ]);
      setConversations(items);
      setMemories(memoryItems);
      setSearches(searchItems);
      setUsage(usageItems);
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }

  // 读取目标账号的单个会话详情。
  async function selectConversation(item: AdminConversation) {
    if (!selected) return;
    setError("");
    try {
      setConversation(
        await api<AdminConversationDetail>(
          `/admin/users/${selected.id}/conversations/${item.id}`,
        ),
      );
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }

  return (
    <div className="page-scroll admin-page">
      <div className="admin-header">
        <div>
          <p className="section-eyebrow">管理员后台</p>
          <h2>账号管理</h2>
        </div>
        <span className="readonly-badge">仅查看，不会替用户发送消息</span>
      </div>
      {error && <div className="global-error" role="alert">{error}</div>}
      {notice && <div className="admin-notice" role="status">{notice}</div>}

      <section className="admin-card">
        <div className="admin-section-heading">
          <div><h3>用户账号</h3><p>创建账号、查看使用情况，或调整账号状态。</p></div>
          <button className="admin-create-toggle" type="button" onClick={toggleCreateForm} aria-expanded={createOpen}>
            {createOpen ? "收起" : "创建账号"}
          </button>
        </div>
        {createOpen && <form className="admin-create-form" onSubmit={createUser}>
          <label>
            <span>用户名</span>
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="3–32 位小写字母、数字或下划线"
              autoFocus
              required
            />
          </label>
          <label>
            <span>临时密码</span>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="至少 10 个字符"
              required
            />
          </label>
          <label>
            <span>角色</span>
            <select value={role} onChange={(event) => setRole(event.target.value as "user" | "admin")}>
              <option value="user">普通用户</option>
              <option value="admin">管理员</option>
            </select>
          </label>
          <button className="admin-primary" type="submit" disabled={busy}>
            {busy ? "正在创建…" : "创建用户"}
          </button>
        </form>}
      </section>

      <section className="admin-card">
        <h3>全部账号</h3>
        <div className="admin-user-list">
          {users.map((user) => (
            <article
              key={user.id}
              className={`admin-user ${selected?.id === user.id ? "selected" : ""}`}
            >
              <div>
                <strong>{user.username}</strong>
                <span className={`account-state ${user.status}`}>
                  {user.status === "active" ? "有效" : "已停用"}
                </span>
                <small>{user.role === "admin" ? "管理员" : "普通用户"}</small>
                {user.id === currentUserId && <small>当前账号</small>}
                {user.must_change_password && <small>待首次改密</small>}
              </div>
              <div className="admin-user-actions">
                <button className="admin-user-primary" type="button" onClick={() => void selectUser(user)}>查看用户数据</button>
                <details className="admin-user-menu">
                  <summary aria-label={`${user.username} 的更多操作`}>更多</summary>
                  <div>
                    <button type="button" onClick={() => void toggleUser(user)}>
                      {user.status === "active" ? "停用账号" : "启用账号"}
                    </button>
                    {user.id !== currentUserId && (
                      <button type="button" onClick={() => startPasswordReset(user)}>设置临时密码</button>
                    )}
                  </div>
                </details>
              </div>
            </article>
          ))}
          {!users.length && <p className="admin-empty">还没有账号。</p>}
        </div>
      </section>

      {selected && (
        <section className="admin-card">
          <div className="admin-detail-heading">
            <div>
              <p className="section-eyebrow">用户数据</p>
              <h3>{selected.username} 的数据</h3>
            </div>
            <span className="readonly-badge">只读</span>
          </div>
          <nav className="admin-detail-tabs" role="tablist" aria-label="用户数据类型">
            {([
              ["conversations", "对话记录"],
              ["memories", "长期记忆"],
              ["search", "搜索记录"],
              ["usage", "调用用量"],
            ] as const).map(([value, label]) => (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={detailTab === value}
                className={detailTab === value ? "active" : ""}
                onClick={() => setDetailTab(value)}
              >
                {label}
              </button>
            ))}
          </nav>

          {detailTab === "conversations" && (
            <div className="admin-conversation-layout">
              <div className="admin-conversation-list-column">
                <h4>对话记录</h4>
                {conversations.map((item) => (
                  <button
                    key={item.id}
                    className={`admin-conversation ${conversation?.id === item.id ? "active" : ""}`}
                    type="button"
                    onClick={() => void selectConversation(item)}
                  >
                    {item.title}
                  </button>
                ))}
                {!conversations.length && <p className="admin-empty">这个账号还没有对话。</p>}
              </div>
              <div className="admin-conversation-view">
                {conversation ? <>
                  <h4>{conversation.title}</h4>
                  {conversation.messages.map((message) => (
                    <article className={`admin-message ${message.role}`} key={message.id}>
                      <small>
                        {message.role === "user" ? "用户" : "助手"} · {({ complete: "已完成", failed: "失败", stopped: "已停止", generating: "回答中" } as Record<string, string>)[message.status] || message.status}
                      </small>
                      <p>{message.content || "（空）"}</p>
                      {!!message.attachments.length && (
                        <ul>
                          {message.attachments.map((attachment) => (
                            <li key={attachment.id}>
                              <a href={`/api/admin/users/${selected.id}/conversations/${conversation.id}/attachments/${attachment.id}/file`}>
                                {attachment.filename}
                              </a>
                            </li>
                          ))}
                        </ul>
                      )}
                    </article>
                  ))}
                </> : <div className="admin-empty-state"><strong>选择一段对话</strong><p>从左侧选择后，可以查看消息和附件。</p></div>}
              </div>
            </div>
          )}

          {detailTab === "memories" && <div className="admin-activity-list">
            {memories.map((item) => (
              <div className="admin-row" key={item.id}>
                <span>{item.fact || "（正文不可读）"}</span>
                <small>{item.status}</small>
              </div>
            ))}
            {!memories.length && <div className="admin-empty-state"><strong>还没有长期记忆</strong><p>用户开始对话后，符合条件的内容会显示在这里。</p></div>}
          </div>}

          {detailTab === "search" && <div className="admin-activity-list">
            {searches.map((item) => (
              <div className="admin-row" key={item.id}>
                <span>{item.query || "（查询已清除）"}</span>
                <small>{item.status}</small>
              </div>
            ))}
            {!searches.length && <div className="admin-empty-state"><strong>还没有搜索记录</strong><p>助手联网搜索后，查询记录会显示在这里。</p></div>}
          </div>}

          {detailTab === "usage" && <div className="admin-activity-list">
            {usage.map((item) => (
              <div className="admin-row" key={item.id}>
                <span>{item.purpose} · {item.model}</span>
                <small>{item.total_tokens ?? item.credits ?? "未返回用量"}</small>
              </div>
            ))}
            {!usage.length && <div className="admin-empty-state"><strong>还没有调用记录</strong><p>模型调用产生后，用量会显示在这里。</p></div>}
          </div>}
        </section>
      )}

      {passwordTarget && (
        <div className="admin-dialog-backdrop">
          <section className="admin-dialog" role="dialog" aria-modal="true" aria-labelledby="reset-password-title">
            <h3 id="reset-password-title">为 {passwordTarget.username} 设置临时密码</h3>
            <p>用户下次登录时必须修改这个密码。</p>
            <form onSubmit={submitPasswordReset}>
              <label>临时密码<input type="password" value={temporaryPassword} onChange={changeTemporaryPassword} minLength={10} autoComplete="new-password" autoFocus required /></label>
              {error && <p role="alert" className="notice error">{error}</p>}
              <div className="admin-dialog-actions">
                <button type="button" className="secondary-button" onClick={closePasswordReset}>取消</button>
                <button type="submit" className="admin-primary" disabled={!temporaryPassword || busy}>保存临时密码</button>
              </div>
            </form>
          </section>
        </div>
      )}
    </div>
  );
}
