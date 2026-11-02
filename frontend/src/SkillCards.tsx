import { useEffect, useRef, useState } from 'react';
import { api, errorMessage, type SkillDescription } from './api';

type Props = { selected: string[]; disabled: boolean; onChange: (names: string[]) => void };

/** 仅在聊天选择器中隐藏 ppt-master，后端目录及历史记录保持可用。 */
function isVisibleSkill(skill: SkillDescription) {
  return skill.name !== 'ppt-master';
}

/** 展示允许手动选择的已安装技能；选择仅改变本条草稿，不创建任务。 */
export function SkillCards({ selected, disabled, onChange }: Props) {
  const [skills, setSkills] = useState<SkillDescription[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const epoch = useRef(0);
  const refreshedAt = useRef(0);

  /** 读取并过滤聊天技能目录，丢弃过期请求；失败时保留选择供用户处理。 */
  async function refresh() {
    const request = ++epoch.current;
    setLoading(true); setError('');
    try {
      const rows = await api<SkillDescription[]>('/skills');
      if (request !== epoch.current) return;
      setSkills(rows.filter(isVisibleSkill)); refreshedAt.current = Date.now();
    } catch (failure) { if (request === epoch.current) setError(errorMessage(failure)); }
    finally { if (request === epoch.current) setLoading(false); }
  }

  /** 挂载时获取目录，回到页面时刷新过期缓存，卸载后不接收旧账号响应。 */
  useEffect(function loadSkills() {
    void refresh();
    /** 页面重新获得焦点时按一分钟有效期刷新，不在每次键入时请求。 */
    function revisit() { if (Date.now() - refreshedAt.current > 60000) void refresh(); }
    window.addEventListener('focus', revisit);
    /** 让请求失效并移除页面焦点监听。 */
    return function cleanup() { epoch.current += 1; window.removeEventListener('focus', revisit); };
  }, []);

  /** 点击切换选择并保留选择顺序，原有输入框文字不受影响。 */
  function toggle(event: React.MouseEvent<HTMLButtonElement>) {
    const name = event.currentTarget.dataset.skill!;
    if (selected.includes(name)) {
      const next: string[] = [];
      for (const item of selected) if (item !== name) next.push(item);
      onChange(next);
    } else onChange([...selected, name]);
  }

  /** 已在输入框编辑时，指针选择卡片不夺走焦点，触屏键盘也可保持展开。 */
  function preserveInput(event: React.PointerEvent<HTMLButtonElement>) {
    if (document.activeElement instanceof HTMLTextAreaElement) event.preventDefault();
  }

  /** 目录中每个真实技能渲染为仅含名称的紧凑切换卡片。 */
  function renderSkill(skill: SkillDescription) {
    const active = selected.includes(skill.name);
    return <article className={`skill-choice${active ? ' is-selected' : ''}`} key={skill.name}>
      <button type="button" className="skill-choice-toggle" data-skill={skill.name} onClick={toggle} onPointerDown={preserveInput}
        aria-label={`选择技能 ${skill.name}`} aria-pressed={active} disabled={disabled}>
        <span className="skill-choice-name">{skill.name}</span>
        <span className="skill-choice-check" aria-hidden="true">
          <svg viewBox="0 0 16 16" fill="none"><path d="m4 8 2.5 2.5L12 5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
        </span>
      </button>
    </article>;
  }

  /** 用户可显式清空失效选择，目录暂不可读时也不会被选择状态困住。 */
  function clear() { onChange([]); }

  if (!loading && !error && !skills.length && !selected.length) return null;

  return <section className="skill-picker" aria-label="选择技能">
    <div className="skill-picker-heading"><span>技能</span>
      {!!selected.length && <button type="button" className="skill-picker-clear" onClick={clear} disabled={disabled}>清除选择</button>}
    </div>
    {loading && !skills.length && <p className="skill-picker-status" role="status">正在读取技能…</p>}
    {error && <p className="skill-picker-status" role="status">{error} <button type="button" onClick={refresh}>重试</button></p>}
    {!loading && !error && !skills.length && <p className="skill-picker-status">暂无可用技能</p>}
    <div className="skill-choice-grid">{skills.map(renderSkill)}</div>
  </section>;
}

/** 聊天历史只显示本条消息实际选择的 Skill 名称。 */
export function MessageSkills({ names }: { names: string[] }) {
  /** 为每个名称生成轻量标签，不重复保存或展开 Skill 正文。 */
  function renderSkill(name: string) {
    return <span key={name} className="message-skill-tag">{name}</span>;
  }
  if (!names.length) return null;
  return <div className="message-skill-tags" aria-label="本条消息选择的技能">{names.map(renderSkill)}</div>;
}
