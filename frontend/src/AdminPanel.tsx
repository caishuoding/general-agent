import { useEffect, useState, type FormEvent } from "react";

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

// 展示管理员账号管理和跨账号只读内容，所有操作都通过后端权限校验。
export function AdminPanel() {
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
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }

  // 停用或重新启用账号，并刷新列表。
  async function toggleUser(user: AdminUser) {
    setError("");
    try {
      await api<AdminUser>(`/admin/users/${user.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          status: user.status === "active" ? "disabled" : "active",
        }),
      });
      await refreshUsers();
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }

  // 通过一次性提示输入临时密码，重置后目标用户必须改密。
  async function resetPassword(user: AdminUser) {
    const temporary = window.prompt(`为 ${user.username} 设置临时密码`);
    if (!temporary) return;
    setError("");
    try {
      await api<AdminUser>(`/admin/users/${user.id}/reset-password`, {
        method: "POST",
        body: JSON.stringify({ password: temporary }),
      });
      await refreshUsers();
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }

  // 读取目标账号的只读内容；切换账号时清空旧详情。
  async function selectUser(user: AdminUser) {
    setSelected(user);
    setConversation(null);
    setError("");
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
          <h2>账号与只读内容</h2>
        </div>
        <span className="readonly-badge">只读查看，不代用户操作</span>
      </div>
      {error && <div className="global-error" role="alert">{error}</div>}

      <section className="admin-card">
        <h3>创建账号</h3>
        <form className="admin-create-form" onSubmit={createUser}>
          <label>
            <span>用户名</span>
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="3–32 位小写字母、数字或下划线"
              required
            />
          </label>
          <label>
            <span>临时密码</span>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
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
            {busy ? "正在创建…" : "创建账号"}
          </button>
        </form>
      </section>

      <section className="admin-card">
        <h3>账号列表</h3>
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
                {user.must_change_password && <small>待首次改密</small>}
              </div>
              <div className="admin-user-actions">
                <button type="button" onClick={() => void selectUser(user)}>查看内容</button>
                <button type="button" onClick={() => void toggleUser(user)}>
                  {user.status === "active" ? "停用" : "启用"}
                </button>
                <button type="button" onClick={() => void resetPassword(user)}>重置密码</button>
              </div>
            </article>
          ))}
          {!users.length && <p className="admin-empty">还没有账号。</p>}
        </div>
      </section>

      {selected && (
        <section className="admin-card">
          <h3>{selected.username} 的只读内容</h3>
          <div className="admin-detail-grid">
            <div>
              <h4>会话</h4>
              {conversations.map((item) => (
                <button
                  key={item.id}
                  className="admin-conversation"
                  type="button"
                  onClick={() => void selectConversation(item)}
                >
                  {item.title}
                </button>
              ))}
              {!conversations.length && <p className="admin-empty">没有会话。</p>}
            </div>
            <div>
              <h4>长期记忆</h4>
              {memories.map((item) => (
                <div className="admin-row" key={item.id}>
                  <span>{item.fact || "（正文不可读）"}</span>
                  <small>{item.status}</small>
                </div>
              ))}
              {!memories.length && <p className="admin-empty">没有记忆。</p>}
            </div>
            <div>
              <h4>搜索记录</h4>
              {searches.map((item) => (
                <div className="admin-row" key={item.id}>
                  <span>{item.query || "（查询已清除）"}</span>
                  <small>{item.status}</small>
                </div>
              ))}
              {!searches.length && <p className="admin-empty">没有搜索记录。</p>}
            </div>
            <div>
              <h4>调用用量</h4>
              {usage.map((item) => (
                <div className="admin-row" key={item.id}>
                  <span>{item.purpose} · {item.model}</span>
                  <small>{item.total_tokens ?? item.credits ?? "未返回用量"}</small>
                </div>
              ))}
              {!usage.length && <p className="admin-empty">没有调用记录。</p>}
            </div>
          </div>
          {conversation && (
            <div className="admin-conversation-detail">
              <h4>{conversation.title}</h4>
              {conversation.messages.map((message) => (
                <article className={`admin-message ${message.role}`} key={message.id}>
                  <small>{message.role === "user" ? "用户" : "助手"} · {message.status}</small>
                  <p>{message.content || "（空）"}</p>
                  {!!message.attachments.length && (
                    <ul>
                      {message.attachments.map((attachment) => (
                        <li key={attachment.id}>
                          <a
                            href={`/api/admin/users/${selected.id}/conversations/${conversation.id}/attachments/${attachment.id}/file`}
                          >
                            {attachment.filename}
                          </a>
                        </li>
                      ))}
                    </ul>
                  )}
                </article>
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
