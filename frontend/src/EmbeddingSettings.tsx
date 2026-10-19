import { useEffect, useState, type ChangeEvent, type FormEvent } from "react";
import { api, errorMessage, type Settings } from "./api";

type EmbeddingSettingsValue = Settings & { dimensions: number | null };
type Props = { settings: Settings };

// 管理独立嵌入连接；保存只更新本机配置，实际连接错误由后续记忆调用反馈。
export function EmbeddingSettings({ settings }: Props) {
  const [embedding, setEmbedding] = useState<EmbeddingSettingsValue | null>(null);
  const [reuse, setReuse] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  // 进入设置页或聊天配置变化后重新读取当前嵌入配置。
  useEffect(() => {
    let active = true;
    // 读取独立嵌入连接，并忽略离开页面后到达的响应。
    async function load() {
      try {
        const next = await api<EmbeddingSettingsValue>("/settings/embedding");
        if (active) setEmbedding(next);
      } catch (failure) {
        if (active) setError(errorMessage(failure));
      }
    }
    void load();
    return () => { active = false; };
  }, [settings]);

  // 切换显式复用模式，模型名称和维度仍由用户独立填写。
  function changeReuse(event: ChangeEvent<HTMLInputElement>) {
    setReuse(event.currentTarget.checked);
  }

  // 保存供独立 Mem0 服务使用的嵌入配置，保存阶段不发起云端请求。
  async function saveEmbedding(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setBusy(true); setError(""); setNotice("");
    try {
      const body = { ...Object.fromEntries(data), dimensions: Number(data.get("dimensions")), reuse_chat_connection: reuse };
      const saved = await api<EmbeddingSettingsValue>("/settings/embedding", {
        method: "PUT",
        body: JSON.stringify(body),
      });
      const key = form.elements.namedItem("api_key") as HTMLInputElement;
      key.value = "";
      setEmbedding(saved);
      setReuse(false);
      setNotice("嵌入配置已保存；重启 Mem0 服务后自动应用。");
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }

  return <div className="settings-card">
    <div className="card-heading"><h3>嵌入连接</h3><span className="small-label">独立模型与维度</span></div>
    <p className="field-help">嵌入服务用于 Mem0；自动提取复用当前聊天模型。附件和历史消息采用本机文字检索。聊天模型不一定支持嵌入接口。</p>
    {embedding ? <form key={`${embedding.base_url}:${embedding.model}:${embedding.dimensions}`} onSubmit={saveEmbedding}>
      <label className="checkbox-field"><input type="checkbox" checked={reuse} onChange={changeReuse} disabled={!settings.api_key_configured || busy} />复用已保存聊天服务的地址与密钥</label>
      <label htmlFor="embedding-base-url">嵌入 API 地址</label>
      <input id="embedding-base-url" name="base_url" type="url" defaultValue={embedding.base_url} disabled={reuse} required={!reuse} maxLength={2048} placeholder="https://your-provider.example/v1" />
      <label htmlFor="embedding-model">嵌入模型名称</label>
      <input id="embedding-model" name="model" defaultValue={embedding.model} required maxLength={200} />
      <label htmlFor="embedding-key">嵌入 API 密钥</label>
      <input id="embedding-key" name="api_key" type="password" autoComplete="off" disabled={reuse} placeholder={embedding.api_key_configured ? "已配置 · 同地址留空保留" : "填写嵌入服务密钥"} />
      <label htmlFor="embedding-dimensions">嵌入维度</label>
      <input id="embedding-dimensions" name="dimensions" type="number" min={1} max={65536} step={1} defaultValue={embedding.dimensions ?? ""} required />
      <p className="field-help">填写服务商声明的向量维度。复用操作复制连接信息；后续修改聊天连接不会自动改动嵌入连接。</p>
      <div className="form-footer"><span>保存配置不会发出云端请求</span><button className="primary-button" disabled={busy}>{busy ? "正在保存…" : "保存嵌入配置"}</button></div>
    </form> : <p className="field-help">正在读取嵌入配置…</p>}
    {error && <p className="notice error" role="alert">{error}</p>}
    {notice && <p className="notice success" role="status">{notice}</p>}
  </div>;
}
