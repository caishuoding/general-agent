import { useEffect, useState, type FormEvent } from "react";
import { SearchSettings } from "./SearchSettings";
import { EmbeddingSettings } from "./EmbeddingSettings";
import { api, errorMessage, type Settings, type Usage } from "./api";

type Props = {
  settings: Settings;
  usage?: Usage[];
  onSaved: (value: Settings) => void;
};

// 管理聊天、独立嵌入与搜索设置以及真实调用用量；保存不代表验证通过。
export function SettingsPanel({ settings, usage, onSaved }: Props) {
  const [recentUsage, setRecentUsage] = useState(usage ?? []);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  // 同步页面首次进入后加载的调用用量。
  useEffect(() => { setRecentUsage(usage ?? []); }, [usage]);

  // 搜索连接保存后刷新最近调用记录。
  async function searchSaved() {
    if (usage !== undefined) setRecentUsage(await api<Usage[]>("/usage"));
  }

  // 读取连接与文档读取预算，将数字字段转为整数后保存并清空密钥。
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
        body: JSON.stringify({ ...Object.fromEntries(data),
          context_window_tokens: Number(data.get("context_window_tokens")),
          output_reserve_tokens: Number(data.get("output_reserve_tokens")),
        }),
      });
      onSaved(next);
      const keyInput = form.elements.namedItem("api_key") as HTMLInputElement;
      keyInput.value = "";
      setNotice("配置已保存，后续调用会使用新连接。");
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setSaving(false);
    }
  }

  // 按实际状态和计量单位显示调用，搜索credits与模型token缺失时均标为未知。
  function renderUsage(call: Usage) {
    return (
      <tr key={call.id}>
        <td>{call.model}<small className="usage-purpose">{call.purpose}</small></td>
        <td>
          <span className={`usage-state ${call.status}`}>
            {({ complete: "已完成", failed: "失败", stopped: "已停止", generating: "生成中" } as Record<string, string>)[call.status] || call.status}
          </span>
        </td>
        <td>{call.unit === "credits" ? `${call.credits ?? "未知"} credits` : call.prompt_tokens ?? "未知"}</td>
        <td>{call.unit === "credits" ? "—" : call.completion_tokens ?? "未知"}</td>
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
          <label htmlFor="context-window">模型上下文窗口（Token）</label>
          <input id="context-window" name="context_window_tokens" type="number" min={1024} max={10000000} required defaultValue={settings.context_window_tokens ?? 32768} />
          <label htmlFor="output-reserve">回答预留（Token）</label>
          <input id="output-reserve" name="output_reserve_tokens" type="number" min={1} max={1000000} required defaultValue={settings.output_reserve_tokens ?? 4096} />
          <p className="field-help">按模型实际能力填写，用于限制文档原文读取量。</p>
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
      <EmbeddingSettings settings={settings} />
      <SearchSettings onSaved={searchSaved} />
      {usage !== undefined && <div className="settings-card usage-card">
        <div className="card-heading">
          <h3>最近调用</h3>
          <span className="small-label">本应用</span>
        </div>
        <p className="field-help">
          最近 100 次实际调用及服务商返回的用量。缺失用量显示为未知。
        </p>
        {recentUsage.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>模型</th>
                  <th>状态</th>
                  <th>输入 / credits</th>
                  <th>输出</th>
                  <th>时间</th>
                </tr>
              </thead>
              <tbody>{recentUsage.map(renderUsage)}</tbody>
            </table>
          </div>
        ) : (
          <div className="empty-usage">
            还没有调用记录，开始一次对话后会显示在这里。
          </div>
        )}
      </div>}
    </section>
  );
}
