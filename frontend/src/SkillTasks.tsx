import { useEffect, useState } from 'react';
import { api, errorMessage } from './api';

type Task = { id: string; skill: string; status: string; artifacts: { id: string; filename: string }[] };

/** 展示会话制作进度和真实交付文件，刷新后仍可读取已保存任务。 */
export function SkillTasks({ conversationId }: { conversationId: string }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [error, setError] = useState('');
  /** 仅在所属会话打开期间刷新任务，切换会话后丢弃旧响应。 */
  useEffect(function loadTasks() {
    let active = true;
    setTasks([]); setError('');
    /** 读取真实任务状态，不从助手文字猜测是否完成。 */
    async function refresh() {
      try { const rows = await api<Task[]>(`/conversations/${conversationId}/skill-tasks`); if (active) { setTasks(rows); setError(''); } }
      catch (failure) { if (active) setError(errorMessage(failure)); }
    }
    void refresh();
    const timer = window.setInterval(refresh, 5000);
    /** 离开会话后清理刷新定时器。 */
    return function cleanup() { active = false; window.clearInterval(timer); };
  }, [conversationId]);

  /** 真实交付使用服务端授权地址下载。 */
  function renderTask(task: Task) {
    const labels: Record<string, string> = { active: '制作中', waiting: '等待下一步', interrupted: '已中断，可继续', revoked: '资料已撤销' };
    /** 依据交付身份生成下载入口，不使用模型生成的磁盘路径。 */
    function renderArtifact(file: Task['artifacts'][number]) {
      return <li key={file.id}><a href={`/api/conversations/${conversationId}/skill-artifacts/${file.id}/file`} download>{file.filename}</a></li>;
    }
    return <details key={task.id} className="skill-task"><summary>演示文稿 · {labels[task.status] || task.status}</summary>
      {task.status !== 'revoked' && <ul>{task.artifacts.map(renderArtifact)}</ul>}
      {task.status === 'waiting' && <p className="field-help">在对话中给出下一步要求，助手会继续这个任务。</p>}
    </details>;
  }
  if (!tasks.length && !error) return null;
  return <aside aria-label="制作任务">{tasks.map(renderTask)}{error && <p role="status">{error}</p>}</aside>;
}
