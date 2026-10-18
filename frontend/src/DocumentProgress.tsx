import type { DocumentProcessing } from './api';

type Props = { progress: DocumentProcessing; status: string };

// 展示本次资料任务的持久进度和读取范围，暂停、失败或停止均不声称处理完成。
export function DocumentProgress({ progress, status }: Props) {
  const state = status === 'paused' ? '等待确认' : status === 'stopped' ? '已停止' : status === 'failed' ? '未完成' : progress.status === 'complete' ? '已完成' : progress.status === 'ready' ? '正在形成最终回答' : '正在处理资料';

  // 缺口与解析限制作为纯文本呈现，不让文档内容成为可执行指令。
  function renderNote(note: string, index: number) {
    return <p key={index}>{note}</p>;
  }

  // 逐文件显示计划覆盖的文字量，并如实列出未读取的页、内容或图片。
  function renderFile(file: DocumentProcessing['files'][number]) {
    return <li key={file.attachment_id}><strong>{file.filename} · {file.chunks} 个可处理文字片段</strong>{[...file.missing, ...file.limitations].map(renderNote)}</li>;
  }

  return <section className="document-progress" aria-label="资料处理进度" role="region">
    <div role="status">{progress.mode === 'compare' ? '资料比较' : '全文总结'} · 已完成 {progress.completed} / {progress.total} 步 · {state}</div>
    <progress value={progress.completed} max={progress.total} aria-label="资料处理完成度" />
    {progress.coverage === 'partial' && <p>部分资料有读取缺口，请结合下方范围说明核对结论。</p>}
    <details><summary>本次资料范围</summary><ul>{progress.files.map(renderFile)}</ul></details>
  </section>;
}
