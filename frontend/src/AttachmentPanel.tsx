import { useEffect, useRef, useState, type ChangeEvent } from 'react';
import { api, errorMessage, type Attachment } from './api';

type Props = { conversationId: string | null; selected: string[]; onSelect: (ids: string[]) => void; disabled: boolean };

// 管理当前会话文本与图片上传和处理状态，明确展示图片读取范围与能力要求。
export function AttachmentPanel({ conversationId, selected, onSelect, disabled }: Props) {
  const [files, setFiles] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const current = useRef(conversationId);
  current.current = conversationId;

  useEffect(
    // 切换会话后重读已保存资料，旧会话慢响应不能覆盖新会话。
    function loadAttachments() {
      let alive = true;
      let timer: ReturnType<typeof setTimeout> | undefined;
      setFiles([]);
      setError('');
      // 处理期间轮询真实状态，所有文件终结后停止轮询。
      async function refresh() {
        if (!conversationId) return;
        try {
          const rows = await api<Attachment[]>(`/conversations/${conversationId}/attachments`);
          if (!alive) return;
          setFiles(rows);
          if (rows.some(
            // 只有尚未处理完的文件需要继续刷新。
            (row) => row.status === 'processing',
          )) timer = setTimeout(refresh, 500);
        } catch (failure) {
          if (alive) setError(errorMessage(failure));
        }
      }
      void refresh();
      // 卸载或切换时停止计时并丢弃旧响应。
      return () => { alive = false; if (timer) clearTimeout(timer); };
    },
    [conversationId, uploading],
  );

  // 将所选文件真实上传到本机会话，并等待后端处理状态决定是否可发送。
  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const chosen = Array.from(event.target.files || []);
    event.target.value = '';
    if (!conversationId || !chosen.length || uploading || disabled) return;
    if (chosen.length + selected.length > 5) { setError('每条消息最多五个附件，请先取消部分选择。'); return; }
    const target = conversationId;
    const body = new FormData();
    for (const file of chosen) body.append('files', file);
    setUploading(true);
    setError('');
    try {
      const rows = await api<Attachment[]>(`/conversations/${target}/attachments`, { method: 'POST', body });
      if (current.current !== target) return;
      onSelect([...selected, ...rows.map(
        // 新上传的文件默认随下一条消息发送，失败文件会阻止发送并可取消选择。
        (row) => row.id,
      )]);
    } catch (failure) {
      if (current.current === target) setError(errorMessage(failure));
    } finally {
      setUploading(false);
    }
  }

  // 仅调整本次消息的附件选择，不删除会话中持久保存的原文件。
  function selectFile(event: ChangeEvent<HTMLInputElement>) {
    const id = event.currentTarget.value;
    if (event.currentTarget.checked && selected.length >= 5) { setError('每条消息最多五个附件。'); return; }
    onSelect(event.currentTarget.checked ? [...selected, id] : selected.filter(
      // 取消本次选择仍允许以后在原会话继续追问。
      (item) => item !== id,
    ));
  }

  // 以真实处理状态及图片尺寸展示附件，出处和下载始终带当前会话归属。
  function renderFile(file: Attachment) {
    const label = file.status === 'ready' ? '可用' : file.status === 'failed' ? '失败' : '处理中';
    return <li key={file.id} className={`attachment-item ${file.status}`}>
      <label><input type="checkbox" aria-label={`随消息发送 ${file.filename}`} value={file.id} checked={selected.includes(file.id)} onChange={selectFile} disabled={disabled || uploading} /><span>{label} · {file.filename}</span></label>
      <a href={`/api/conversations/${conversationId}/attachments/${file.id}/file`} download>原文件</a>
      {file.image_metadata && <small className="attachment-image-info">{file.image_metadata.format} · {file.image_metadata.width} × {file.image_metadata.height} 像素</small>}
      {file.error && <p role="alert">{file.error}</p>}
    </li>;
  }

  return <section className="attachment-panel" aria-label="当前会话资料">
    <div className="attachment-heading"><label className={`upload-label ${disabled || !conversationId ? 'disabled' : ''}`}>＋ 添加资料<input aria-label="上传会话附件" type="file" accept=".txt,.md,.markdown,.png,.jpg,.jpeg" multiple onChange={upload} disabled={disabled || !conversationId || uploading} /></label><span>{conversationId ? 'TXT / Markdown / PNG / JPEG · 每条消息最多 5 个 · 每个 20 MB' : '新建会话后可上传资料'}</span></div>
    {conversationId && <p className="attachment-image-help">文档图表或截图可单独上传为图片。选中图片随消息发送，或在原会话明确提问图片；不需要读图时请取消选择。使用前请在设置检查图片能力。</p>}
    {uploading && <p role="status">正在上传文件…</p>}
    {error && <p role="alert">{error}</p>}
    {!!files.length && <details open><summary>本会话资料（{files.length}） · 本次选择 {selected.length} 个</summary><ul>{files.map(renderFile)}</ul></details>}
  </section>;
}
