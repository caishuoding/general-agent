import { useEffect, useState, type FormEvent } from "react";
import { api, errorMessage, type Settings, type Usage } from "./api";

type Price = { base_url: string; model: string; input_per_million: string | null; output_per_million: string | null; per_credit: string | null };
type Budget = { monthly_limit: string | null; prices: Price[] };
type Summary = { month: string; known_cost: string; estimated_cost: string | null; calls: number; unknown_calls: number; pending_calls: number };
type Props = { settings: Settings; usage: Usage[]; onUsageChanged: (usage: Usage[]) => void };

// 管理人民币预算、按服务模型绑定的单价，以及上海自然月全量统计。
export function BudgetPanel({ settings, usage, onUsageChanged }: Props) {
  const [budget, setBudget] = useState<Budget | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  // 获取预算和当前连接，自动提供当前聊天与嵌入模型的价格输入项。
  async function load() {
    const [saved, totals, embedding] = await Promise.all([api<Budget>("/settings/budget"), api<Summary>("/usage/summary"), api<Settings>("/settings/embedding")]);
    for (const config of [settings, embedding]) {
      if (config.model && !saved.prices.some(
        // 价格只匹配相同服务地址和模型，不继承其他模型的单价。
        (price) => price.base_url === config.base_url && price.model === config.model,
      )) saved.prices.push({ base_url: config.base_url, model: config.model, input_per_million: null, output_per_million: null, per_credit: null });
    }
    setBudget(saved); setSummary(totals);
  }

  useEffect(
    // 能力检查或连接更新后刷新统计及价格表。
    () => { void load().catch(
      // 加载失败时展示公开错误，不伪造费用为零。
      (failure: unknown) => setError(errorMessage(failure)),
    ); }, [settings, usage],
  );

  // 从表单读取十进制字符串，空值保持未知且空预算表示关闭限制。
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!budget) return;
    const data = new FormData(event.currentTarget);
    setBusy(true); setError(""); setNotice("");
    try {
      const prices = budget.prices.map(
        // 保留模型身份，只更新用户明确填写的单价。
        (price, index) => ({ ...price, input_per_million: data.get(`${index}-input`) || null, output_per_million: data.get(`${index}-output`) || null, per_credit: price.per_credit }),
      );
      await api("/settings/budget", { method: "PUT", body: JSON.stringify({ monthly_limit: data.get("monthly_limit") || null, prices }) });
      await load(); onUsageChanged(await api<Usage[]>("/usage")); setNotice("预算与单价已保存。暂停的请求可以重新检查。");
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }

  // 展示按模型独立填写的输入与输出单价，嵌入只要求输入单价。
  function renderPrice(price: Price, index: number) {
    return <fieldset key={`${price.base_url}:${price.model}`} className="price-fields">
      <legend>{price.model}</legend><p className="field-help">{price.base_url}</p>
      <label>{price.model} 输入单价<input name={`${index}-input`} aria-label={`${price.model} 输入单价`} type="number" min="0" step="any" defaultValue={price.input_per_million ?? ""} placeholder="未知" /></label>
      <label>{price.model} 输出单价<input name={`${index}-output`} aria-label={`${price.model} 输出单价`} type="number" min="0" step="any" defaultValue={price.output_per_million ?? ""} placeholder="嵌入可留空" /></label>
    </fieldset>;
  }

  return <div className="settings-card budget-card">
    <div className="card-heading"><h3>用量与月度预算</h3><span className="small-label">本应用估算</span></div>
    <p className="field-help">金额均为人民币元，单价单位为元 / 百万 token。若服务商使用其他币种，请先自行换算。估算费用不等于服务商实际账单，也不是服务商扣费上限。</p>
    {summary && <div className="monthly-summary" data-testid="monthly-summary">
      <strong>{summary.month} · 上海时区</strong>
      <p>{summary.estimated_cost === null ? `费用未知；已知部分 ¥${summary.known_cost}` : `本月估算费用 ¥${summary.estimated_cost}`}</p>
      <span>共 {summary.calls} 次调用 · {summary.unknown_calls} 次费用未知 · {summary.pending_calls} 次等待用量</span>
    </div>}
    {budget && <form onSubmit={save}>
      <label htmlFor="monthly-limit">月度预算（人民币元）</label>
      <input id="monthly-limit" name="monthly_limit" type="number" min="0" step="any" defaultValue={budget.monthly_limit ?? ""} placeholder="留空不限制" />
      <p className="field-help">留空关闭限制，0 表示每次调用前暂停。达到预算、缺单价或费用未知时暂停新调用；单次请求可能使总额超过预算。</p>
      {budget.prices.map(renderPrice)}
      <p className="field-help">单价仅用于对应服务和模型；嵌入只填输入单价。补齐价格会补算已有完整用量，已知费用保留原单价。缺失用量无法补算。</p>
      <button className="primary-button" disabled={busy}>保存预算与单价</button>
    </form>}
    {error && <p className="notice error" role="alert">{error}</p>}
    {notice && <p className="notice success" role="status">{notice}</p>}
  </div>;
}
