import { useEffect, useState, type ChangeEvent, type MouseEvent } from 'react';
import { api, errorMessage } from './api';

type Impact = { name: string; revision: string; memories: { id: string; fact: string; status: string; can_keep: boolean }[] };
type Result = { id: string; status: string; phase: string; error: string | null; kind: string };
export type DeleteTarget = { path: string; label: string };

// 删除前展示真实关联事实，默认删除全部；失败保留同一清理身份供刷新后恢复。
export function SourceDeletion({ target, onClose, onChanged }: { target: DeleteTarget; onClose: () => void; onChanged: () => Promise<void> }) {
  const [impact, setImpact] = useState<Impact | null>(null);
  const [keep, setKeep] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(
    // 每次打开重新读取影响版本，旧页面不能默许后来新增的关联记忆。
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
  // 取消删除勾选意味着明确保留该完整事实，未完整保存的项不能保留。
  function choose(event: ChangeEvent<HTMLInputElement>) {
    const id = event.currentTarget.value;
    setKeep(event.currentTarget.checked ? keep.filter(
      // 恢复默认删除时移除该保留身份。
      (value) => value !== id,
    ) : [...keep, id]);
  }
  // 发送具体影响版本和逐项保留选择，503也刷新真实停用状态与恢复入口。
  async function remove() {
    if (!impact) return;
    setBusy(true); setError('');
    try {
      const response = await fetch(`/api${target.path}`, { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ preview_revision: impact.revision, keep_memory_ids: keep }) });
      const result = await response.json();
      if (!response.ok && !result.id) throw new Error(result.detail || '删除未被接受，请重新预览。');
      await onChanged(); onClose();
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  // 每条可保留记忆都以明确勾选表达默认删除，禁用不完整事实的保留能力。
  function renderMemory(memory: Impact['memories'][number]) {
    return <label key={memory.id} className="source-delete-memory"><input type="checkbox" value={memory.id} checked={!keep.includes(memory.id)} onChange={choose} disabled={busy || !memory.can_keep} />删除关联记忆：{memory.fact || '尚未完整保存的记忆'} {!memory.can_keep && '（不能保留未完成项）'}</label>;
  }
  return <section role="dialog" aria-label="删除来源影响预览" className="source-delete-dialog">
    <h2>删除{target.label}</h2>
    <p>{impact?.name}。原文件和派生资料将清除。删除会话也会移除其消息；只删除附件会保留已完成的聊天正文。</p>
    <p>以下记忆默认一起删除。取消勾选可保留已完整保存的事实，并标记“来源已删除”。</p>
    {impact && (impact.memories.length ? impact.memories.map(renderMemory) : <p>没有关联的长期记忆。</p>)}
    {error && <p role="alert">{error}</p>}
    <button onClick={remove} disabled={busy || !impact}>{busy ? '正在清理…' : '确认删除来源'}</button><button onClick={onClose} disabled={busy}>取消</button>
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
  // 沿原批准身份恢复，不重新选择关联事实或重复申请云预算。
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
    return <p key={row.id}>{row.kind === 'conversation' ? '会话' : '附件'}已停用，清理尚未完成。{row.error}<button data-id={row.id} onClick={retry} disabled={!!busy}>重试来源清理</button></p>;
  }
  return <div className="source-delete-pending" role="status">{rows.map(renderResult)}{error && <p role="alert">{error}</p>}</div>;
}
