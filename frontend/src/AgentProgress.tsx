import { useEffect, useState } from "react";

import type { MessageProgress } from "./api";

type Props = {
  progress: MessageProgress | null | undefined;
  startedAt: string;
};

/** 将秒数格式化为适合进度行展示的简短中文时长。 */
function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${minutes} 分 ${String(remainder).padStart(2, "0")} 秒`;
}

/** 显示当前工具进度和总耗时，避免在长任务中让用户误以为界面卡住。 */
export function AgentProgress({ progress, startedAt }: Props) {
  const [now, setNow] = useState(Date.now());

  useEffect(
    // 生成期间每秒刷新耗时；卸载后立即停止计时器。
    function tickElapsed() {
      // 用浏览器当前时间重新计算，避免定时器漂移累积。
      const timer = window.setInterval(function updateClock() {
        setNow(Date.now());
      }, 1000);
      return function stopTimer() {
        window.clearInterval(timer);
      };
    },
    [startedAt],
  );

  const started = new Date(startedAt).getTime();
  const elapsed = Number.isFinite(started) ? Math.max(0, Math.floor((now - started) / 1000)) : 0;
  return (
    <div className="agent-progress" role="status" aria-live="polite">
      <span className="agent-progress-dot" aria-hidden="true" />
      <span className="agent-progress-label">{progress?.label || "正在准备回答…"}</span>
      <span className="agent-progress-time">已用时 {formatElapsed(elapsed)}</span>
    </div>
  );
}
