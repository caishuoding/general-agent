import { useEffect, useState, type FormEvent } from 'react';
import { api, errorMessage, type Settings } from './api';

type Props = { onSaved: () => Promise<void> };

// 管理独立 DeepSeek 搜索与抓取凭据，保存或删除不会触发联网请求。
export function SearchSettings({ onSaved }: Props) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  useEffect(
    // 仅从公开设置读取地址和配置状态，卸载后忽略旧响应。
    () => {
      let active = true;
      // 加载不含密钥的独立搜索配置。
      async function load() {
        try { const saved = await api<Settings>('/settings/search'); if (active) setSettings(saved); }
        catch (failure) { if (active) setError(errorMessage(failure)); }
      }
      void load();
      // 页面离开后不再写入状态。
      return () => { active = false; };
    }, [],
  );
  // 保存当前搜索连接并清空密钥输入，同步刷新调用记录。
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    setBusy(true); setError(''); setNotice('');
    try {
      setSettings(await api<Settings>('/settings/search', { method: 'PUT', body: JSON.stringify(Object.fromEntries(new FormData(form))) }));
      (form.elements.namedItem('api_key') as HTMLInputElement).value = '';
      await onSaved(); setNotice('搜索配置已保存；助手会根据当前请求按需自主联网。');
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  // 移除本机搜索密钥，其他模型配置仍独立保存。
  async function clear() {
    setBusy(true); setError(''); setNotice('');
    try { setSettings(await api<Settings>('/settings/search', { method: 'DELETE' })); await onSaved(); setNotice('搜索密钥已移除。'); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  return <div className="settings-card">
    <div className="card-heading"><h3>联网搜索</h3><span className="small-label">DeepSeek Search / Fetch · 独立配置</span></div>
    <p className="field-help">助手会根据当前请求自主决定何时联网，搜索词和抓取地址直接发送给 DeepSeek，无需额外确认。查询可能包含当前会话附件或长期记忆中的个人信息。未配置不会妨碍其他功能。</p>
    {settings && <form key={settings.base_url} onSubmit={save}>
      <label>DeepSeek API 地址<input name="base_url" type="url" defaultValue={settings.base_url} required maxLength={2048} /></label>
      <label>DeepSeek API 密钥<input name="api_key" type="password" autoComplete="off" placeholder={settings.api_key_configured ? '已配置 · 同地址留空保留' : '填写独立 DeepSeek 密钥'} /></label>
      <p className="field-help">仅在本机后端保存，读取接口不返回密钥。搜索返回摘要，网页抓取返回 Markdown；成功且有内容的请求计为一次用量。</p>
      <div className="form-footer"><button type="button" className="secondary-button" onClick={clear} disabled={busy || !settings.api_key_configured}>移除搜索密钥</button><button className="primary-button" disabled={busy}>保存搜索配置</button></div>
    </form>}
    {notice && <p role="status" className="notice success">{notice}</p>}
    {error && <p role="alert" className="notice error">{error}</p>}
  </div>;
}
