import { useEffect, useRef, useState, type FormEvent, type ChangeEvent } from "react";
import { api, errorMessage, type Settings, type Usage } from "./api";

type EmbeddingSettings = Settings & { dimensions: number | null };
type Capability = "text" | "image" | "tools" | "embedding";
type Result = {
  status: "unverified" | "passed" | "failed";
  detail: string;
  model: string;
  checked_at: string | null;
};
type Results = Record<Capability, Result>;
type Props = { settings: Settings; onUsageChanged: (usage: Usage[]) => void };
const labels: Record<Capability, string> = { text: "文字", image: "图片", tools: "工具调用", embedding: "嵌入" };
const capabilities: Capability[] = ["text", "image", "tools", "embedding"];

// 管理独立嵌入表单与四项能力结果，只展示服务对已保存配置的实际验证状态。
export function ModelCapabilities({ settings, onUsageChanged }: Props) {
  const version = useRef(0);
  const [embedding, setEmbedding] = useState<EmbeddingSettings | null>(null);
  const [results, setResults] = useState<Results | null>(null);
  const [reuse, setReuse] = useState(false);
  const [busy, setBusy] = useState<Capability | "save" | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  // 每次聊天配置保存后重新读取当前版本，离开页面时忽略过期读取。
  useEffect(() => {
    let active = true;
    version.current += 1;
    setResults(null);
    // 同时读取独立嵌入配置和按配置版本筛选的能力结果。
    async function load() {
      try {
        const [nextEmbedding, nextResults] = await Promise.all([
          api<EmbeddingSettings>("/settings/embedding"),
          api<Results>("/settings/capabilities"),
        ]);
        if (active) {
          setEmbedding(nextEmbedding);
          setResults(nextResults);
        }
      } catch (failure) {
        if (active) setError(errorMessage(failure));
      }
    }
    void load();
    // 防止卸载后的请求覆盖后续页面状态。
    return () => { active = false; };
  }, [settings]);

  // 切换显式复用模式，模型名称和维度始终由用户独立填写。
  function changeReuse(event: ChangeEvent<HTMLInputElement>) {
    setReuse(event.currentTarget.checked);
  }

  // 保存供独立 Mem0 服务使用的嵌入配置，不在业务库维护向量索引。
  async function saveEmbedding(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setBusy("save"); setError(""); setNotice("");
    try {
      const body = { ...Object.fromEntries(data), dimensions: Number(data.get("dimensions")), reuse_chat_connection: reuse };
      const saved = await api<EmbeddingSettings>("/settings/embedding", {
        method: "PUT",
        body: JSON.stringify(body),
      });
      const key = form.elements.namedItem("api_key") as HTMLInputElement;
      key.value = "";
      setEmbedding(saved);
      onUsageChanged(await api<Usage[]>("/usage"));
      setReuse(false);
      setResults(await api<Results>("/settings/capabilities"));
      setNotice("嵌入配置已保存；重启 Mem0 服务后自动应用，新配置需要单独检查。");
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(null);
    }
  }

  // 发起单项可能计费的检查，再读取当前配置状态，避免晚到结果冒充新配置。
  async function check(capability: Capability) {
    const checkingVersion = version.current;
    setBusy(capability); setError(""); setNotice("");
    try {
      await api<Result>(`/settings/capabilities/${capability}`, { method: "POST" });
      const currentResults = await api<Results>("/settings/capabilities");
      if (checkingVersion === version.current) setResults(currentResults);
      onUsageChanged(await api<Usage[]>("/usage"));
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(null);
    }
  }

  // 渲染单项检查及其可解释状态，互不合并为一个成功标识。
  function renderCapability(capability: Capability) {
    const result = results?.[capability];
    const status = result?.status ?? "unverified";
    // 把按钮事件转换为指定能力的独立检查。
    function runCheck() { void check(capability); }
    return (
      <div className="capability-row" key={capability} data-testid={`capability-${capability}`}>
        <div>
          <strong>{labels[capability]}</strong>
          <span className={`capability-state ${status}`}>{status === "passed" ? "通过" : status === "failed" ? "失败" : "未验证"}</span>
          <p className="field-help">{result?.detail ?? "正在读取当前配置…"}</p>
          {result?.checked_at && <p className="field-help">{result.model} · {new Date(result.checked_at).toLocaleString("zh-CN")}</p>}
        </div>
        <button type="button" className="secondary-button" disabled={busy !== null || !results || !(capability === "embedding" ? embedding?.api_key_configured : settings.api_key_configured)} onClick={runCheck}>
          {busy === capability ? "检查中…" : `检查${labels[capability]}`}
        </button>
      </div>
    );
  }

  return <>
    <div className="settings-card">
      <div className="card-heading"><h3>嵌入连接</h3><span className="small-label">独立模型与维度</span></div>
      <p className="field-help">嵌入服务用于 Mem0；自动提取复用当前聊天模型。附件和历史消息采用本机文字检索。聊天模型不一定支持嵌入接口。</p>
      {embedding ? <form key={`${embedding.base_url}:${embedding.model}:${embedding.dimensions}`} onSubmit={saveEmbedding}>
        <label className="checkbox-field"><input type="checkbox" checked={reuse} onChange={changeReuse} disabled={!settings.api_key_configured || busy !== null} />复用已保存聊天服务的地址与密钥</label>
        <label htmlFor="embedding-base-url">嵌入 API 地址</label>
        <input id="embedding-base-url" name="base_url" type="url" defaultValue={embedding.base_url} disabled={reuse} required={!reuse} maxLength={2048} placeholder="https://your-provider.example/v1" />
        <label htmlFor="embedding-model">嵌入模型名称</label>
        <input id="embedding-model" name="model" defaultValue={embedding.model} required maxLength={200} />
        <label htmlFor="embedding-key">嵌入 API 密钥</label>
        <input id="embedding-key" name="api_key" type="password" autoComplete="off" disabled={reuse} placeholder={embedding.api_key_configured ? "已配置 · 同地址留空保留" : "填写嵌入服务密钥"} />
        <label htmlFor="embedding-dimensions">嵌入维度</label>
        <input id="embedding-dimensions" name="dimensions" type="number" min={1} max={65536} step={1} defaultValue={embedding.dimensions ?? ""} required />
        <p className="field-help">填写服务商声明的向量维度，检查时会核对实际返回值。复用操作复制连接信息；后续修改聊天连接不会自动改动嵌入连接。</p>
        <div className="form-footer"><span>保存配置不会发出云端请求</span><button className="primary-button" disabled={busy !== null}>{busy === "save" ? "正在保存…" : "保存嵌入配置"}</button></div>
      </form> : <p className="field-help">正在读取嵌入配置…</p>}
    </div>
    <div className="settings-card">
      <div className="card-heading"><h3>能力检查</h3><span className="small-label">分别验证</span></div>
      <p className="field-help">每次检查都会发出可能计费的请求，并记录可获取的用量。检查只针对已保存配置，请先保存修改；更新配置后旧结果失效。</p>
      {capabilities.map(renderCapability)}
      {error && <p className="notice error" role="alert">{error}</p>}
      {notice && <p className="notice success" role="status">{notice}</p>}
    </div>
  </>;
}
