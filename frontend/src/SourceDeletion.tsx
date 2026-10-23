import { useEffect, useState, type MouseEvent } from 'react';
import { api, errorMessage } from './api';

type Impact = { name: string; revision: string };
type Result = { id: string; status: string; phase: string; error: string | null; kind: string };
export type DeleteTarget = { path: string; label: string };

// 删除前只确认来源本身，关联数据由后端默认清理且不在前端展示。
export function SourceDeletion({ target, onClose, onChanged }: { target: DeleteTarget; onClose: () => void; onChanged: () => Promise<void> }) {
  const [impact, setImpact] = useState<Impact | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(
    // 每次打开重新读取影响版本，避免按过期预览删除后来变化的来源。
    () => {
      let active = true;
      // 仅显示仍然打开的弹窗请求结果。
      async function load() {
        try { const result = await api<Impact>(`${target.path}/deletion-preview`); if (active) setImpact(result); }
        catch (failure) { if (active) setError(errorMessage(failure)); }
      }
      void load();
      // 关闭后忽略迟到预览。
      return () => { active = false; };
    }, [target.path],
  );
  // 发送具体影响版本并默认清理全部关联数据，503 也刷新停用状态与恢复入口。
  async function remove() {
    if (!impact) return;
    setBusy(true); setError('');
    try {
      const response = await fetch(`/api${target.path}`, { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ preview_revision: impact.revision, keep_memory_ids: [] }) });
      const result = await response.json();
      if (!response.ok && !result.id) throw new Error(result.detail || '删除未被接受，请重新预览。');
      await onChanged(); onClose();
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  return <section role="dialog" aria-label="删除来源影响预览" className="source-delete-dialog">
    <h2>确认删除{target.label}？</h2>
    <p>{impact?.name} 会被删除，原文件和关联资料也会一并清理。删除附件时，已经完成的聊天正文会保留。</p>
    {error && <p role="alert">{error}</p>}
    <div className="source-delete-actions">
      <button className="danger-button" onClick={remove} disabled={busy || !impact}>{busy ? '正在删除…' : '删除'}</button>
      <button onClick={onClose} disabled={busy}>取消</button>
    </div>
  </section>;
}

// 来源隐藏或删除后仍展示未完成清理，重启刷新不会丢失恢复入口。
export function SourceDeletionPending({ revision, onChanged }: { revision: number; onChanged: () => Promise<void> }) {
  const [rows, setRows] = useState<Result[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  useEffect(
    // 打开空间和每次删除后读取无正文的持久清理记录。
    () => {
      let active = true;
      // 读取失败明确提示，不把无法加载视为已清理。
      async function load() {
        try { const result = await api<Result[]>('/source-deletions'); if (active) setRows(result); }
        catch (failure) { if (active) setError(errorMessage(failure)); }
      }
      void load();
      // 离开页面不应用旧响应。
      return () => { active = false; };
    }, [revision],
  );
  // 沿原删除身份恢复，不重新选择关联事实或创建第二个清理任务。
  async function retry(event: MouseEvent<HTMLButtonElement>) {
    const id = event.currentTarget.dataset.id;
    if (!id) return;
    setBusy(id); setError('');
    try { const response = await fetch(`/api/source-deletions/${id}/retry`, { method: 'POST' }); const result = await response.json(); if (!response.ok && !result.id) throw new Error(result.detail); await onChanged(); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(''); }
  }
  // 无正文的阶段提示让停用与物理清理完成保持区别。
  function renderResult(row: Result) {
    return <p key={row.id}>{row.kind === 'conversation' ? '会话' : '附件'}已停用，但清理还没完成。{row.error}<button data-id={row.id} onClick={retry} disabled={!!busy}>继续清理</button></p>;
  }
  return <div className="source-delete-pending" role="status">{rows.map(renderResult)}{error && <p role="alert">{error}</p>}</div>;
}
