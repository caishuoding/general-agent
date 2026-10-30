import { useEffect, useState, type FormEvent } from 'react';
import { api, errorMessage } from './api';

type ProviderStatus = { configured: string[]; skills: { name: string; description: string; version: string }[] };

/** 管理完整 Skill 清单和可选媒体提供商配置，服务端不回显变量值。 */
export function SkillSettings() {
  const [status, setStatus] = useState<ProviderStatus>({ configured: [], skills: [] });
  const [configuration, setConfiguration] = useState('');
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');

  /** 首次打开设置时读取已安装 Skill 及已配置变量名。 */
  useEffect(function loadSkillSettings() {
    let active = true;
    /** 读取公开配置状态，卸载后丢弃迟到结果。 */
    async function load() {
      try { const value = await api<ProviderStatus>('/settings/skill-providers'); if (active) setStatus(value); }
      catch (failure) { if (active) setError(errorMessage(failure)); }
    }
    void load();
    /** 取消当前页面对迟到响应的更新。 */
    return function cleanup() { active = false; };
  }, []);

  /** 按 KEY=value 解析提供商配置；空值保留原配置，不向服务端发送应用环境。 */
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(''); setNotice('');
    try {
      const variables: Record<string, string> = {};
      for (const source of configuration.split('\n')) {
        const line = source.trim();
        if (!line || line.startsWith('#')) continue;
        const separator = line.indexOf('=');
        if (separator <= 0) throw new Error('请按每行 KEY=value 填写配置。');
        variables[line.slice(0, separator).trim()] = line.slice(separator + 1).trim();
      }
      setStatus(await api<ProviderStatus>('/settings/skill-providers', { method: 'PUT', body: JSON.stringify({ variables }) }));
      setConfiguration(''); setNotice('配置已保存，后续制作命令会使用新配置。');
    } catch (failure) { setError(errorMessage(failure)); }
  }

  /** 清除所有可选提供商配置，保留已安装 Skill。 */
  async function clear() {
    try {
      setStatus(await api<ProviderStatus>('/settings/skill-providers', { method: 'PUT', body: JSON.stringify({ remove: status.configured }) }));
      setNotice('提供商配置已清除。');
    } catch (failure) { setError(errorMessage(failure)); }
  }

  /** 展示上游完整能力描述，不将其摘要成固定功能列表。 */
  function renderSkill(skill: ProviderStatus['skills'][number]) {
    return <details key={skill.name}><summary>{skill.name} · {skill.version}</summary><p>{skill.description}</p></details>;
  }

  return <section className="settings-card">
    <h3>Skills 与媒体服务</h3>
    {status.skills.map(renderSkill)}
    <p className="field-help">制作 PPT 时助手会自主加载所需 Skill。图片生成、语音等功能可在此配置提供商；普通文字和形状制作无需额外服务密钥。</p>
    <p>已配置变量：{status.configured.join('、') || '无'}</p>
    <form onSubmit={save}>
      <label htmlFor="skill-providers">提供商配置（每行 KEY=value）</label>
      <textarea id="skill-providers" value={configuration} onChange={
        /** 更新本次待保存配置，保存成功后立即清空。 */
        function changeConfiguration(event) { setConfiguration(event.target.value); }
      } rows={6} autoComplete="off" spellCheck={false} placeholder={'IMAGE_BACKEND=openai\nOPENAI_BASE_URL=https://你的服务地址/v1\nOPENAI_MODEL=你的图片模型\nOPENAI_API_KEY=你的密钥'} />
      <p className="field-help">保存仅更新填写的非空变量，不回显密钥；具体变量遵循 PPT Master 的提供商配置说明。</p>
      <button type="submit">保存媒体服务配置</button>
      <button type="button" onClick={clear} disabled={!status.configured.length}>清除媒体服务配置</button>
    </form>
    {notice && <p role="status">{notice}</p>}{error && <p role="alert">{error}</p>}
  </section>;
}
