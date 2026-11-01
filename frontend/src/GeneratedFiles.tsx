import { useEffect, useState } from 'react';
import { api, errorMessage } from './api';

type GeneratedFile = { id: string; filename: string; media_type: string; created_at: string };

/** 展示当前会话真实发布的生成文件，不引入制作任务或内部工作路径。 */
export function GeneratedFiles({ conversationId }: { conversationId: string }) {
  const [files, setFiles] = useState<GeneratedFile[]>([]);
  const [error, setError] = useState('');
  /** 仅在所属会话打开期间刷新文件，切换会话后丢弃旧响应。 */
  useEffect(function loadFiles() {
    let active = true;
    setFiles([]); setError('');
    /** 读取当前账号会话的文件清单，不从助手文字推断交付结果。 */
    async function refresh() {
      try {
        const rows = await api<GeneratedFile[]>(`/conversations/${conversationId}/generated-files`);
        if (active) { setFiles(rows); setError(''); }
      } catch (failure) {
        if (active) setError(errorMessage(failure));
      }
    }
    void refresh();
    const timer = window.setInterval(refresh, 5000);
    /** 离开会话后停止轮询并忽略迟到响应。 */
    return function cleanup() { active = false; window.clearInterval(timer); };
  }, [conversationId]);

  /** 为真实文件生成服务端授权下载入口。 */
  function renderFile(file: GeneratedFile) {
    return <li key={file.id}><a href={`/api/conversations/${conversationId}/generated-files/${file.id}/file`} download>{file.filename}</a></li>;
  }
  if (!files.length && !error) return null;
  return <aside aria-label="生成文件">{!!files.length && <details className="generated-files" open><summary>生成文件</summary><ul>{files.map(renderFile)}</ul></details>}{error && <p role="status">{error}</p>}</aside>;
}
