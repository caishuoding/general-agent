import { useEffect, useRef, useState, type ChangeEvent } from 'react';
import { api, errorMessage, type Attachment } from './api';

type Props = {
  conversationId: string | null;
  disabled: boolean;
  revision: number;
  resetToken: number;
  onDelete: (id: string, name: string) => void;
};

// 上传资料并只展示本次上传的临时状态，不读取或列出会话历史附件。
export function AttachmentPanel({ conversationId, disabled, revision, resetToken, onDelete }: Props) {
  const [files, setFiles] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const current = useRef(conversationId);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const generation = useRef(0);
  const lastRevision = useRef(revision);
  const lastReset = useRef(resetToken);
  current.current = conversationId;

  useEffect(
    // 切换会话时丢弃旧会话的上传状态，避免把临时文件带到新会话。
    function clearForConversation() {
      if (timer.current) clearTimeout(timer.current);
      generation.current += 1;
      setFiles([]);
      setError('');
    },
    [conversationId],
  );

  useEffect(
    // 来源清理后清空临时状态，避免继续展示后端已删除的文件。
    function clearAfterSourceChange() {
      if (revision === lastRevision.current) return;
      lastRevision.current = revision;
      if (timer.current) clearTimeout(timer.current);
      generation.current += 1;
      setFiles([]);
      setError('');
    },
    [revision],
  );

  useEffect(
    // 消息成功发送后清空托盘，历史归属改由该条用户消息展示。
    function clearAfterSend() {
      if (resetToken === lastReset.current) return;
      lastReset.current = resetToken;
      if (timer.current) clearTimeout(timer.current);
      generation.current += 1;
      setFiles([]);
      setError('');
    },
    [resetToken],
  );

  useEffect(
    // 组件卸载时停止尚未结束的状态轮询。
    function stopPollingOnUnmount() {
      return () => { if (timer.current) clearTimeout(timer.current); };
    },
    [],
  );

  // 轮询本次上传文件的真实处理状态，切换会话后丢弃旧响应。
  async function pollUploads(ids: string[], target: string, pollGeneration: number) {
    try {
      const rows = await api<Attachment[]>(`/conversations/${target}/attachments`);
      if (current.current !== target || generation.current !== pollGeneration) return;
      const currentRows = rows.filter(
        // 只更新仍在本次上传托盘中的文件，不把历史资料带回界面。
        (row) => ids.includes(row.id),
      );
      setFiles(
        // 用后端状态替换对应临时项，其他上传批次保持不变。
        (items) => items.map(
          (item) => currentRows.find((row) => row.id === item.id) ?? item,
        ),
      );
      if (currentRows.some(
        // 仍有文件处理中时继续读取真实状态。
        (row) => row.status === 'processing',
      )) timer.current = setTimeout(() => void pollUploads(ids, target, pollGeneration), 500);
    } catch (failure) {
      if (current.current === target) setError(errorMessage(failure));
    }
  }

  // 将所选文件上传到当前会话，只把本次返回的记录加入临时托盘。
  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const chosen = Array.from(event.target.files || []);
    event.target.value = '';
    if (!conversationId || !chosen.length || uploading || disabled) return;
    if (chosen.length > 5) {
      setError('每次最多上传五个文件，请重新选择。');
      return;
    }
    const target = conversationId;
    const body = new FormData();
    for (const file of chosen) body.append('files', file);
    setUploading(true);
    setError('');
    try {
      const rows = await api<Attachment[]>(`/conversations/${target}/attachments`, { method: 'POST', body });
      if (current.current !== target) return;
      const ids = [...files.map((file) => file.id), ...rows.map((row) => row.id)];
      setFiles(
        // 合并多次上传，同时按附件标识避免重复展示。
        (items) => [...items.filter((item) => !rows.some((row) => row.id === item.id)), ...rows],
      );
      if (rows.some((row) => row.status === 'failed')) setError('部分附件解析失败，可查看原因后重新上传。');
      if (rows.some((row) => row.status === 'processing')) {
        if (timer.current) clearTimeout(timer.current);
        timer.current = setTimeout(() => void pollUploads(ids, target, generation.current), 500);
      }
    } catch (failure) {
      if (current.current === target) setError(errorMessage(failure));
    } finally {
      setUploading(false);
    }
  }

  // 将后端声明的缺口和能力范围作为纯文本展示，不把部分文字提取称为全文理解。
  function renderNotice(notice: string) {
    return <p key={notice}>{notice}</p>;
  }

  // 展示本次上传文件的真实状态；删除中的文件不能读取。
  function renderFile(file: Attachment) {
    const info = file.parse_info;
    // 以当前文件的独立身份打开删除预览。
    function removeFile() { onDelete(file.id, file.filename); }
    const label = file.status === 'ready' ? (info?.format ? (info.coverage === 'partial' ? '文字可用（部分）' : '文字可用') : '可用') : file.status === 'failed' ? '失败' : file.status === 'deleting' ? '清理未完成' : '处理中';
    return <li key={file.id} className={`attachment-chip ${file.status}`}>
      <span className="attachment-chip-main">
        <span>{label} · {file.filename}</span>
        {file.status !== 'deleting' && <button type="button" className="attachment-remove" aria-label={`删除附件 ${file.filename}`} onClick={removeFile}>×</button>}
      </span>
      {file.image_metadata && <small className="attachment-image-info">{file.image_metadata.format} · {file.image_metadata.width} × {file.image_metadata.height} 像素</small>}
      {file.error && <small role="alert">{file.error}</small>}
      {info?.format && <small>文字提取：{info.readable_units || 0} / {info.total_units || 0} {info.format === 'pdf' ? '页' : '段落'}</small>}
      {info?.missing_units?.map(renderNotice)}
      {info?.limitations?.map(renderNotice)}
    </li>;
  }

  return <section className="attachment-composer" aria-label="添加资料">
    <div className="attachment-tools">
      <label className={`upload-label ${disabled || !conversationId ? 'disabled' : ''}`} title="TXT / Markdown / PDF / DOCX / PNG / JPEG；每次最多 5 个，每个 20 MB，PDF 最多 200 页">＋ 添加资料<input aria-label="上传会话附件" type="file" accept=".txt,.md,.markdown,.pdf,.docx,.png,.jpg,.jpeg" multiple onChange={upload} disabled={disabled || !conversationId || uploading} /></label>
      <span className="attachment-hint">{conversationId ? '资料自动用于本会话问答 · 每次最多 5 个' : '新建会话后可上传资料'}</span>
    </div>
    {uploading && <p role="status">正在上传文件…</p>}
    {error && <p role="alert">{error}</p>}
    {!!files.length && <ul className="attachment-tray" aria-label="本次上传资料">{files.map(renderFile)}</ul>}
  </section>;
}
