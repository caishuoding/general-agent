import { useEffect, useRef, useState } from 'react';
import { api, errorMessage } from './api';

export type RebuildImpact = { requires_rebuild: boolean; memory_count: number; target: { base_url: string; model: string; dimensions: number }; collections: { kind: string; name: string; affected: boolean }[] };
export type PendingRebuild = { body: Record<string, unknown>; impact: RebuildImpact; id: string };
type Job = { id: string; status: string; phase: string; total: number; completed: number; excluded: number; queryable: boolean; target: { model: string; dimensions: number }; active: { model: string; dimensions: number } | null; error: string | null; pause: { operation_id: string; pause_id: string; detail: string } | null };
type Props = { pending: PendingRebuild | null; onDismiss: () => void; onPublished: () => Promise<void> };
const labels: Record<string, string> = { running: '正在重建', paused: '等待预算许可', failed: '重建失败', complete: '重建完成', cancelling: '正在取消并清理目标', cancelled: '已取消重建' };
const phases: Record<string, string> = { initializing: '等待在途记忆操作完成', verifying: '验证目标嵌入服务', embedding: '逐条重建已批准记忆', publishing: '核验并切换配置', cleanup: '新索引已可用，正在清理旧索引', complete: '新索引已可查询', cancelled: '继续使用原索引' };

// 显示目标影响、持久任务进度与单次预算许可；刷新后恢复同一任务。
export function EmbeddingRebuild({ pending, onDismiss, onPublished }: Props) {
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const published = useRef('');

  // 接收真实状态，发布完成后同步父表单和已发生用量。
  async function receive(next: Job | null) {
    setJob(next);
    if (next?.status === 'complete' && published.current !== next.id) {
      published.current = next.id;
      await onPublished();
    }
  }

  // 组件挂载时读取最近任务，不因刷新而自动发出新的云请求。
  useEffect(() => {
    let active = true;
    // 首次打开设置读取最近任务；不自动继续云请求。
    async function load() {
      try { const next = await api<Job | null>('/settings/embedding/rebuilds/latest'); if (active) await receive(next); }
      catch (failure) { if (active) setError(errorMessage(failure)); }
    }
    void load();
    // 退出设置后忽略迟到读取。
    return () => { active = false; };
  }, []);

  // 仅在活动任务期间更新进度，状态改变会撤销上一轮计时器。
  useEffect(() => {
    if (!job || !['running', 'cancelling'].includes(job.status)) return;
    let active = true;
    // 轮询仅取本机持久进度，不自动批准或再次触发嵌入。
    async function refresh() {
      try { const next = await api<Job>(`/settings/embedding/rebuilds/${job!.id}`); if (active) await receive(next); }
      catch (failure) { if (active) setError(errorMessage(failure)); }
    }
    const timer = window.setInterval(refresh, 400);
    // 状态改变或卸载时停止本轮轮询。
    return () => { active = false; window.clearInterval(timer); };
  }, [job?.id, job?.status]);

  // 明确确认预览后受理同一稳定身份，密钥只随目标请求发送一次。
  async function start() {
    if (!pending) return;
    setBusy(true); setError('');
    try {
      const next = await api<Job>('/settings/embedding/rebuilds', { method: 'POST', body: JSON.stringify({ id: pending.id, target: pending.body, confirmed: true }) });
      await receive(next); onDismiss();
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }

  // 仅对当前挑战批准一次，随后继续原任务，已完成步骤仍由后端对账。
  async function resume(approve: boolean) {
    if (!job) return;
    setBusy(true); setError('');
    try {
      if (approve && job.pause) await api(`/budget/operations/${job.pause.operation_id}/approve`, { method: 'POST', body: JSON.stringify({ pause_id: job.pause.pause_id }) });
      const next = await api<Job>(`/settings/embedding/rebuilds/${job.id}/resume`, { method: 'POST' });
      setJob(['complete', 'cancelled'].includes(next.status) ? next : { ...next, status: 'running', pause: null });
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }

  // 无批准地重查预算或恢复失败步骤，外部请求仍逐次通过预算。
  function retry() { void resume(false); }
  // 批准范围只覆盖下一次嵌入，不能放行整批记忆。
  function approve() { void resume(true); }
  // 取消先撤销后续调用，界面直到实际目标清理完成才显示已取消。
  async function cancel() {
    if (!job) return;
    setBusy(true); setError('');
    try { await receive(await api<Job>(`/settings/embedding/rebuilds/${job.id}/cancel`, { method: 'POST' })); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  // 按影响清单展示实际向量范围，非向量功能不发起重建。
  function collection(row: RebuildImpact['collections'][number]) { return <li key={row.kind}>{row.name} · {row.affected ? '需要重建' : '不受影响'}</li>; }

  return <>
    {pending && <section className="settings-card" role="dialog" aria-label="嵌入索引重建影响">
      <h3>确认更换嵌入空间</h3>
      <p>{pending.impact.target.model} · {pending.impact.target.dimensions} 维 · {pending.impact.target.base_url}</p>
      <p>本次重建 {pending.impact.memory_count} 条有效长期记忆。正文与条目标识保留；来源已删除但明确保留的记忆也参加重建。</p>
      <ul>{pending.impact.collections.map(collection)}</ul>
      <p className="field-help">验证和每条重新嵌入都可能计费并受预算限制。重建期间暂停长期记忆召回和写入，普通聊天可继续。目标完整可用后才切换。</p>
      <div className="budget-actions"><button disabled={busy} onClick={start}>确认重建索引</button><button disabled={busy} onClick={onDismiss}>返回修改</button></div>
    </section>}
    {job && <section className="settings-card" data-testid="embedding-rebuild" aria-live="polite">
      <div className="card-heading"><h3>长期记忆索引重建</h3><strong>{labels[job.status] || job.status}</strong></div>
      <p>活动：{job.active ? `${job.active.model} · ${job.active.dimensions} 维` : '尚无活动索引'}。目标：{job.target.model} · {job.target.dimensions} 维。</p>
      <p>{phases[job.phase] || job.phase} · 已完成 {job.completed} / {job.total} · 已排除 {job.excluded}</p>
      <progress value={job.completed + job.excluded} max={Math.max(job.total, 1)} aria-label="索引重建进度" />
      <p className="field-help">{job.queryable ? '活动索引可查询。' : '重建尚未完成，长期记忆召回和写入暂停；普通聊天仍可使用。'}</p>
      {job.error && <p role="alert" className="notice error">{job.error}</p>}
      {job.pause && <p className="notice">{job.pause.detail} 每次批准只允许下一次嵌入调用。</p>}
      <div className="budget-actions">
        {job.status === 'paused' && job.pause && <button onClick={approve} disabled={busy}>仅允许下一次嵌入调用</button>}
        {['paused', 'failed'].includes(job.status) && <button onClick={retry} disabled={busy}>继续原重建</button>}
        {!['complete', 'cancelled'].includes(job.status) && !['publishing', 'cleanup'].includes(job.phase) && <button onClick={cancel} disabled={busy}>取消重建</button>}
      </div>
    </section>}
    {error && <p className="notice error" role="alert">{error}</p>}
  </>;
}
