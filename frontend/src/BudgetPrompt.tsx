import { useEffect, useState } from "react";
import { api, errorMessage, type BudgetDecision } from "./api";

type Props = { onConfigure: () => void };

// 在所有功能共用的暂停提示中批准下一次调用、重新检查或取消当前操作。
export function BudgetPrompt({ onConfigure }: Props) {
  const [queue, setQueue] = useState<BudgetDecision[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const current = queue[0];
  useEffect(
    // 监听统一接口的暂停事件，保持并发操作各自独立的批准挑战。
    () => {
      // 接收新暂停操作并排队，禁止后到请求覆盖当前请求。
      function receive(event: Event) {
        const decision = (event as CustomEvent<BudgetDecision>).detail;
        setQueue(
          // 保留已有待处理请求，按到达顺序展示。
          (previous) => [...previous, decision],
        );
      }
      window.addEventListener("budget-paused", receive);
      return (
        // 卸载时停止接收页面已无法处理的暂停事件。
        () => window.removeEventListener("budget-paused", receive)
      );
    }, [],
  );

  // 将当前操作交还原调用方，下一项暂停单独展示。
  function finish(retry: boolean) {
    current.resolve(retry);
    setQueue(
      // 移除已经由用户处理的队首，不改变其他操作。
      (previous) => previous.slice(1),
    );
    setError("");
  }

  // 只向后端批准当前挑战，再使用原操作标识重新提交相同请求。
  async function approve() {
    setBusy(true); setError("");
    try {
      await api(`/budget/operations/${current.operation_id}/approve`, { method: "POST", body: JSON.stringify({ pause_id: current.pause_id }) });
      finish(true);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }

  // 未批准地再次检查预算，适用于补价格、调整预算或等待在途用量后。
  function recheck() { finish(true); }

  // 取消后端操作并结束原请求，已经发出的费用仍保留。
  async function cancel() {
    setBusy(true);
    try {
      await api(`/budget/operations/${current.operation_id}/cancel`, { method: "POST" });
      finish(false);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }

  if (!current) return null;
  return <section className="budget-prompt" role="dialog" aria-label="云端请求已暂停">
    <strong>云端请求已暂停</strong><p>{current.detail}</p>
    <p>批准只允许当前操作的下一次云端调用，后续调用仍会检查预算。已发生费用不会撤销。</p>
    {error && <p role="alert">{error}</p>}
    <div className="budget-actions">
      <button onClick={approve} disabled={busy}>仅允许下一次云端调用</button>
      <button onClick={recheck} disabled={busy}>重新检查预算</button>
      <button onClick={onConfigure} disabled={busy}>打开预算设置</button>
      <button onClick={cancel} disabled={busy}>取消本次操作</button>
    </div>
  </section>;
}
