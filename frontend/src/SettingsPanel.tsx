import { useState, type FormEvent } from "react";
import { api, errorMessage, type Settings, type Usage } from "./api";

type Props = {
  settings: Settings;
  usage: Usage[];
  onSaved: (value: Settings) => void;
};

// 管理模型连接与近期用量；保存配置不被描述为已验证服务能力。
export function SettingsPanel({ settings, usage, onSaved }: Props) {
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  // 从表单读取连接信息，成功后清空密钥输入并更新公开配置状态。
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const next = await api<Settings>("/settings", {
        method: "PUT",
        body: JSON.stringify(Object.fromEntries(data)),
      });
      onSaved(next);
      const keyInput = form.elements.namedItem("api_key") as HTMLInputElement;
      keyInput.value = "";
      setNotice("配置已保存。发送一条消息即可使用这个模型。");
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setSaving(false);
    }
  }

  // 按服务实际返回值显示 token，未取得的用量始终标为未知。
  function renderUsage(call: Usage) {
    return (
      <tr key={call.id}>
        <td>{call.model}</td>
        <td>
          <span className={`usage-state ${call.status}`}>
            {call.status === "complete" ? "已完成" : "失败"}
          </span>
        </td>
        <td>{call.prompt_tokens ?? "未知"}</td>
        <td>{call.completion_tokens ?? "未知"}</td>
        <td>
          {new Date(call.created_at).toLocaleString("zh-CN", {
            month: "numeric",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
          })}
        </td>
      </tr>
    );
  }

  return (
    <section className="settings-page">
      <div className="eyebrow">让助手适合你</div>
      <h2>设置</h2>
      <p className="page-description">
        连接你选择的模型，聊天记录保存在这台电脑上。
      </p>
      <div className="settings-card">
        <div className="card-heading">
          <h3>模型连接</h3>
          <span className="small-label">文字对话</span>
        </div>
        <form onSubmit={save}>
          <label htmlFor="base-url">API 地址</label>
          <input
            id="base-url"
            name="base_url"
            type="url"
            defaultValue={settings.base_url}
            placeholder="https://your-provider.example/v1"
            required
            maxLength={2048}
          />
          <p className="field-help">
            填写服务商提供的基础地址，通常以 /v1 结尾。
          </p>
          <label htmlFor="model">模型名称</label>
          <input
            id="model"
            name="model"
            defaultValue={settings.model}
            placeholder="服务商提供的模型名称"
            required
            maxLength={200}
          />
          <label htmlFor="api-key">API 密钥</label>
          <input
            id="api-key"
            name="api_key"
            type="password"
            autoComplete="off"
            placeholder={
              settings.api_key_configured
                ? "已配置 · 留空保留现有密钥"
                : "填写 API 密钥"
            }
          />
          <p className="field-help">
            密钥仅由本机后端保存。更换 API 地址时，请重新填写密钥。
          </p>
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
          {notice && (
            <p className="notice success" role="status">
              {notice}
            </p>
          )}
          <div className="form-footer">
            <span>调用按服务商规则计费</span>
            <button className="primary-button" disabled={saving}>
              {saving ? "正在保存…" : "保存配置"}
            </button>
          </div>
        </form>
      </div>
      <div className="settings-card usage-card">
        <div className="card-heading">
          <h3>最近调用</h3>
          <span className="small-label">本应用</span>
        </div>
        <p className="field-help">
          展示服务返回的 token 用量。此版本尚未启用费用估算和预算限制。
        </p>
        {usage.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>模型</th>
                  <th>状态</th>
                  <th>输入</th>
                  <th>输出</th>
                  <th>时间</th>
                </tr>
              </thead>
              <tbody>{usage.map(renderUsage)}</tbody>
            </table>
          </div>
        ) : (
          <div className="empty-usage">
            还没有调用记录，开始一次对话后会显示在这里。
          </div>
        )}
      </div>
    </section>
  );
}
