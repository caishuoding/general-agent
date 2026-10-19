import { useState, type FormEvent } from "react";

import { api, errorMessage, type AuthUser } from "./api";

type LoginProps = {
  onAuthenticated: (user: AuthUser) => void;
};

type ChangePasswordProps = {
  user: AuthUser;
  onChanged: (user: AuthUser) => void;
  onLogout: () => void;
};

// 渲染用户名密码登录页，并处理统一登录错误。
export function LoginScreen({ onAuthenticated }: LoginProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // 提交登录信息；成功后把当前账号交给应用根组件。
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      const user = await api<AuthUser>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      onAuthenticated(user);
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-card" aria-labelledby="login-title">
        <div className="auth-brand" aria-hidden="true">✳</div>
        <p className="auth-kicker">个人对话空间</p>
        <h1 id="login-title">登录</h1>
        <p className="auth-description">账号由管理员创建，不开放注册。</p>
        <form className="auth-form" onSubmit={submit}>
          <label className="auth-field">
            <span>用户名</span>
            <input
              autoComplete="username"
              autoFocus
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              required
            />
          </label>
          <label className="auth-field">
            <span>密码</span>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>
          {error && <div className="auth-error" role="alert">{error}</div>}
          <button className="auth-submit" type="submit" disabled={submitting}>
            {submitting ? "正在登录…" : "登录"}
          </button>
        </form>
      </section>
    </main>
  );
}

// 渲染首次登录强制改密页，完成前不进入聊天界面。
export function PasswordChangeScreen({
  user,
  onChanged,
  onLogout,
}: ChangePasswordProps) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // 提交新密码并校验两次输入一致。
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    if (newPassword !== confirmation) {
      setError("两次输入的新密码不一致。");
      return;
    }
    setSubmitting(true);
    try {
      const changed = await api<AuthUser>("/auth/password", {
        method: "POST",
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      });
      onChanged(changed);
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-card" aria-labelledby="change-password-title">
        <div className="auth-brand" aria-hidden="true">✳</div>
        <p className="auth-kicker">首次登录</p>
        <h1 id="change-password-title">设置新密码</h1>
        <p className="auth-description">
          {user.username}，请先把临时密码替换为至少 10 个字符的新密码。
        </p>
        <form className="auth-form" onSubmit={submit}>
          <label className="auth-field">
            <span>当前密码</span>
            <input
              type="password"
              autoComplete="current-password"
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
              required
            />
          </label>
          <label className="auth-field">
            <span>新密码</span>
            <input
              type="password"
              autoComplete="new-password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              required
            />
          </label>
          <label className="auth-field">
            <span>再次输入新密码</span>
            <input
              type="password"
              autoComplete="new-password"
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
              required
            />
          </label>
          {error && <div className="auth-error" role="alert">{error}</div>}
          <button className="auth-submit" type="submit" disabled={submitting}>
            {submitting ? "正在保存…" : "保存新密码"}
          </button>
          <button className="auth-link" type="button" onClick={onLogout}>
            退出登录
          </button>
        </form>
      </section>
    </main>
  );
}
