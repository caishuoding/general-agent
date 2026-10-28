import { useEffect, useState } from "react";

import { AdminPanel } from "./AdminPanel";
import { LoginScreen, PasswordChangeScreen } from "./AuthScreens";
import { SettingsPanel } from "./SettingsPanel";
import {
  AUTH_EXPIRED_EVENT,
  api,
  errorMessage,
  type AuthUser,
  type Settings,
} from "./api";

type AdminTab = "accounts" | "settings";

// 独立管理端：管理员登录、账号管理和系统配置，不进入用户聊天界面。
export function AdminApp() {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [tab, setTab] = useState<AdminTab>("accounts");
  const [changingPassword, setChangingPassword] = useState(false);
  const [settings, setSettings] = useState<Settings>({
    base_url: "",
    model: "",
    api_key_configured: false,
  });
  const [error, setError] = useState("");

  useEffect(
    // 首次进入管理端时只读取管理员登录会话。
    function restoreSession() {
      let mounted = true;
      // 只在组件仍有效时应用会话结果。
      async function load() {
        try {
          const current = await api<AuthUser>("/admin/auth/me");
          if (mounted) setUser(current);
        } catch {
          if (mounted) setUser(null);
        } finally {
          if (mounted) setAuthReady(true);
        }
      }
      void load();
      return () => {
        mounted = false;
      };
    },
    [],
  );

  useEffect(
    // 监听会话失效广播，避免会话被吊销后仍停留在管理界面。
    function watchSessionExpiry() {
      // 会话失效时清空账号并回到管理员登录页。
      function handleExpired() {
        setUser(null);
        setChangingPassword(false);
        setTab("accounts");
        setError("登录已失效，请重新登录。");
      }
      window.addEventListener(AUTH_EXPIRED_EVENT, handleExpired);
      return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handleExpired);
    },
    [],
  );

  useEffect(
    // 管理员登录后加载全局模型、嵌入和搜索配置。
    function loadSettings() {
      if (!user || user.role !== "admin" || user.must_change_password) return;
      let mounted = true;
      // 配置读取失败时保留明确错误，不阻塞账号管理。
      async function load() {
        try {
          const saved = await api<Settings>("/settings");
          if (mounted) setSettings(saved);
        } catch (failure) {
          if (mounted) setError(errorMessage(failure));
        }
      }
      void load();
      return () => {
        mounted = false;
      };
    },
    [user?.id, user?.role, user?.must_change_password],
  );

  // 登录成功后保存管理员账号；普通用户会被后端拒绝登录此入口。
  function handleAuthenticated(next: AuthUser) {
    setError("");
    setUser(next);
    setChangingPassword(false);
  }

  // 首次改密成功后回到管理端。
  function handlePasswordChanged(next: AuthUser) {
    setError("");
    setUser(next);
    setChangingPassword(false);
  }

  // 退出管理端会话并回到管理员登录页。
  async function logout() {
    try {
      await api("/admin/auth/logout", { method: "POST" });
    } catch {
      /* 网络失败也回到登录页，由下一次请求确认会话状态。 */
    } finally {
      setUser(null);
      setChangingPassword(false);
      setTab("accounts");
      setError("");
    }
  }

  if (!authReady) {
    return <div className="auth-loading" role="status">正在检查管理员会话…</div>;
  }
  if (!user || user.role !== "admin") {
    return (
      <LoginScreen
        expectedRole="admin"
        kicker="管理端"
        title="管理员登录"
        description=""
        showStory={false}
        initialError={error}
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
  if (changingPassword) {
    return (
      <PasswordChangeScreen
        user={user}
        forced={false}
        onChanged={handlePasswordChanged}
        onLogout={logout}
        onCancel={() => setChangingPassword(false)}
      />
    );
  }

  return (
    <div className="admin-shell">
      <header className="admin-topbar">
        <div className="admin-title">
          <span className="admin-mark" aria-hidden="true">▦</span>
          <div>
            <strong>管理端</strong>
            <small>账号、数据与系统配置</small>
          </div>
        </div>
        <div className="admin-session">
          <span>{user.username}</span>
          <button type="button" onClick={() => setChangingPassword(true)}>修改密码</button>
          <button type="button" onClick={logout}>退出登录</button>
        </div>
      </header>
      <nav className="admin-tabs" aria-label="管理端页面">
        <button
          type="button"
          className={tab === "accounts" ? "active" : ""}
          onClick={() => setTab("accounts")}
        >
          用户与数据
        </button>
        <button
          type="button"
          className={tab === "settings" ? "active" : ""}
          onClick={() => setTab("settings")}
        >
          系统配置
        </button>
      </nav>
      {error && <div className="global-error" role="alert">{error}</div>}
      {tab === "accounts" ? (
        <AdminPanel />
      ) : (
        <div className="page-scroll">
          <SettingsPanel settings={settings} onSaved={setSettings} />
        </div>
      )}
    </div>
  );
}
