import { useEffect, useRef, useState, type ChangeEvent, type FormEvent, type MouseEvent } from 'react';
import { api, AUTH_EXPIRED_EVENT, errorMessage } from './api';
import './knowledge.css';

type KnowledgeBase = { id: string; name: string; document_count?: number; deleting?: boolean };
type KnowledgeDocument = {
  id: string; filename: string; size: number; status: string; stage: string;
  completed: number; total: number | null; error: string | null; warning: string | null;
};
const stages: Record<string, string> = {
  queued: '等待处理', parsing: '正在读取内容', ocr: '正在识别扫描页', images: '正在理解图片',
  chunking: '正在整理段落', indexing: '正在建立检索', complete: '处理完成',
};

/** 在独立页面管理长期资料，处理进度直接读取服务端事实。 */
export function KnowledgePanel() {
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [name, setName] = useState('');
  const [rename, setRename] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [upload, setUpload] = useState<{ loaded: number; total: number | null } | null>(null);
  const [editingName, setEditingName] = useState(false);
  const [notice, setNotice] = useState('');
  const current = useRef(active);
  const mounted = useRef(true);
  const request = useRef<XMLHttpRequest | null>(null);
  current.current = active;
  const hasDeleting = bases.some(
    /** 待清理对象需要保留可见的重试入口并持续核对后台结果。 */
    (base) => base.deleting,
  );

  useEffect(
    /** 打开页面时载入自己的知识库，卸载后丢弃晚到响应。 */
    function loadBases() {
      mounted.current = true;
      /** 读取知识库列表并选择第一个，保留可见加载与失败状态。 */
      async function load() {
        try {
          const rows = await api<KnowledgeBase[]>('/knowledge-bases');
          if (!mounted.current) return;
          setBases(rows);
          const first = rows.find(
            /** 已撤销的知识库不能进入文档管理与会话选择。 */
            (row) => !row.deleting,
          );
          setActive(first?.id ?? null);
          setRename(first?.name ?? '');
        } catch (failure) {
          if (mounted.current) setError(errorMessage(failure));
        } finally {
          if (mounted.current) setLoading(false);
        }
      }
      void load();
      /** 离开页面停止浏览器上传；已登记的服务端处理继续执行。 */
      return () => { mounted.current = false; request.current?.abort(); };
    }, [],
  );

  useEffect(
    /** 整库删除后继续显示清理状态，失败时用户仍可主动重试。 */
    function watchDeletedBases() {
      if (!hasDeleting) return;
      let cancelled = false;
      /** 查询仍在清理的对象，清理完成后自动移除。 */
      async function refresh() {
        try {
          const rows = await api<KnowledgeBase[]>('/knowledge-bases');
          if (!cancelled) setBases(rows);
        } catch (failure) { if (!cancelled) setError(errorMessage(failure)); }
      }
      const timer = setInterval(refresh, 1500);
      /** 页面离开或清理结束后停止列表刷新。 */
      return () => { cancelled = true; clearInterval(timer); };
    }, [hasDeleting],
  );

  useEffect(
    /** 当前知识库的处理中或删除中资料持续轮询，切换时隔离旧响应。 */
    function watchDocuments() {
      let cancelled = false;
      let timer: ReturnType<typeof setTimeout> | undefined;
      setDocuments([]);
      if (!active) return;
      const target = active;
      /** 只在当前页面有效时应用后端计数，并根据真实状态安排下一次请求。 */
      async function load() {
        try {
          const rows = await api<KnowledgeDocument[]>(`/knowledge-bases/${target}/documents`);
          if (cancelled) return;
          setDocuments(rows);
          if (rows.some(
            /** 仅未完成处理或清理的对象需要继续刷新。 */
            (row) => row.status === 'processing' || row.status === 'deleting',
          )) timer = setTimeout(load, 1000);
        } catch (failure) {
          if (!cancelled) setError(errorMessage(failure));
        }
      }
      void load();
      /** 切换知识库或卸载时停止轮询并忽略正在返回的旧请求。 */
      return () => { cancelled = true; if (timer) clearTimeout(timer); };
    }, [active, revision],
  );

  /** 切换资料列表，上传期间保持目标固定。 */
  function choose(event: MouseEvent<HTMLButtonElement>) {
    const id = event.currentTarget.dataset.id;
    const base = bases.find(
      /** 只接受当前账号已加载的知识库身份。 */
      (item) => item.id === id,
    );
    if (base) {
      setActive(base.id);
      setRename(base.name);
      setEditingName(false);
      setError('');
      setNotice('');
    }
  }

  /** 更新创建知识库的名称草稿。 */
  function changeName(event: ChangeEvent<HTMLInputElement>) { setName(event.target.value); }
  /** 更新当前知识库的重命名草稿。 */
  function changeRename(event: ChangeEvent<HTMLInputElement>) { setRename(event.target.value); }
  /** 进入名称编辑状态，并清理上一次操作的提示。 */
  function startEditingName() {
    setEditingName(true);
    setNotice('');
    setError('');
  }
  /** 取消名称编辑并恢复当前知识库名称。 */
  function cancelEditingName() {
    const current = bases.find(
      /** 只读取当前仍存在的知识库名称。 */
      (item) => item.id === active,
    );
    setRename(current?.name ?? '');
    setEditingName(false);
  }
  /** 创建后进入新知识库，失败保留名称供用户重试。 */
  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!name.trim() || busy) return;
    const form = event.currentTarget;
    setBusy(true); setError('');
    setNotice('');
    try {
      const base = await api<KnowledgeBase>('/knowledge-bases', { method: 'POST', body: JSON.stringify({ name }) });
      if (!mounted.current) return;
      setBases(
        /** 追加服务端返回的真实知识库记录。 */
        (rows) => [...rows, base],
      );
      setActive(base.id); setRename(base.name); setName('');
      setNotice('知识库已创建。');
      form.closest('details')?.removeAttribute('open');
    } catch (failure) { if (mounted.current) setError(errorMessage(failure)); }
    finally { if (mounted.current) setBusy(false); }
  }

  /** 保存名称，不改变当前知识库及其文档身份。 */
  async function saveName(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!active || busy || !rename.trim()) return;
    const target = active;
    setBusy(true); setError('');
    try {
      const saved = await api<KnowledgeBase>(`/knowledge-bases/${target}`, { method: 'PATCH', body: JSON.stringify({ name: rename }) });
      if (mounted.current) setBases(
        /** 用真实响应更新相同知识库，保留其他库的状态。 */
        (rows) => rows.map(
          /** 只替换这次重命名的对象。 */
          (row) => row.id === target ? { ...row, name: saved.name } : row,
        ),
      );
      if (mounted.current) {
        setEditingName(false);
        setNotice('名称已更新。');
      }
    } catch (failure) { if (mounted.current) setError(errorMessage(failure)); }
    finally { if (mounted.current) setBusy(false); }
  }

  /** 用户确认后删除当前知识库；文档清理由后台执行。 */
  async function deleteBase() {
    if (!active || busy || !window.confirm('删除这个知识库及其全部文档？')) return;
    const target = active;
    setBusy(true); setError('');
    try {
      await api(`/knowledge-bases/${target}`, { method: 'DELETE' });
      if (!mounted.current) return;
      const rows = await api<KnowledgeBase[]>('/knowledge-bases');
      if (!mounted.current) return;
      const next = rows.find(
        /** 已撤销访问的库只保留清理重试，不再展示正文。 */
        (row) => !row.deleting,
      );
      setBases(rows); setActive(next?.id ?? null); setRename(next?.name ?? '');
      setEditingName(false);
      setNotice('知识库已删除。');
    } catch (failure) { if (mounted.current) setError(errorMessage(failure)); }
    finally { if (mounted.current) setBusy(false); }
  }

  /** 重新提交整库清理，保留失败对象直至后端真正清理完成。 */
  async function retryBaseCleanup(event: MouseEvent<HTMLButtonElement>) {
    const id = event.currentTarget.dataset.id;
    if (!id || busy) return;
    setBusy(true); setError('');
    setNotice('');
    try { await api(`/knowledge-bases/${id}`, { method: 'DELETE' }); }
    catch (failure) { if (mounted.current) setError(errorMessage(failure)); }
    finally { if (mounted.current) setBusy(false); }
  }

  /** 使用浏览器实际字节进度上传，上传结束后交由服务端阶段轮询。 */
  function uploadFiles(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    if (!active || !files.length || upload || busy) return;
    if (files.length > 5 || files.some(
      /** 上传前提示超限，服务端仍独立执行强制校验。 */
      (file) => file.size > 20_000_000,
    )) { setError('每次最多 5 个文件，单文件最多 20 MB。'); return; }
    const target = active;
    const form = new FormData();
    for (const file of files) form.append('files', file);
    const xhr = new XMLHttpRequest();
    request.current = xhr;
    xhr.open('POST', `/api/knowledge-bases/${target}/documents`);
    xhr.setRequestHeader('X-Operation-ID', crypto.randomUUID());
    setError(''); setUpload({ loaded: 0, total: null });
    setNotice('');
    /** 记录浏览器实际发送字节，未知总大小时保持不确定状态。 */
    xhr.upload.onprogress = function showProgress(progress) {
      if (mounted.current) setUpload({ loaded: progress.loaded, total: progress.lengthComputable ? progress.total : null });
    };
    /** 注册成功后刷新处理状态，错误只展示服务端安全说明。 */
    xhr.onload = function uploadFinished() {
      if (!mounted.current) return;
      setUpload(null); request.current = null;
      if (xhr.status === 401) window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
      if (xhr.status < 200 || xhr.status >= 300) {
        try { setError(JSON.parse(xhr.responseText).detail || '上传失败，请重试。'); }
        catch { setError('上传失败，请重试。'); }
      }
      if (current.current === target) setRevision(
        /** 读取本次新登记文档及后台实际阶段。 */
        (value) => value + 1,
      );
      if (xhr.status >= 200 && xhr.status < 300) setNotice('文件已添加，正在处理。');
    };
    /** 网络错误不伪装成已经入库。 */
    xhr.onerror = function uploadFailed() {
      if (mounted.current) { setError('上传连接中断，请刷新核对后重试。'); setUpload(null); }
    };
    xhr.send(form);
  }

  /** 根据文档按钮身份执行失败重试或删除，不通过文件名推测对象。 */
  async function documentAction(event: MouseEvent<HTMLButtonElement>) {
    const id = event.currentTarget.dataset.id;
    const action = event.currentTarget.dataset.action;
    if (!active || !id || busy) return;
    if (action === 'delete' && !window.confirm('删除这份文档及其切片和图片？')) return;
    const target = active;
    setBusy(true); setError('');
    setNotice('');
    try {
      await api(`/knowledge-bases/${target}/documents/${id}${action === 'retry' ? '/retry' : ''}`,
        { method: action === 'retry' ? 'POST' : 'DELETE' });
      if (mounted.current && current.current === target) setRevision(
        /** 重新查询文档进度，不在界面自行推测处理是否成功。 */
        (value) => value + 1,
      );
      if (mounted.current && current.current === target) {
        setNotice(action === 'delete' ? '文档已删除。' : '已重新开始处理。');
      }
    } catch (failure) { if (mounted.current) setError(errorMessage(failure)); }
    finally { if (mounted.current) setBusy(false); }
  }

  /** 渲染知识库切换按钮。 */
  function renderBase(base: KnowledgeBase) {
    if (base.deleting) return <div key={base.id}><span>{base.name} · 清理中</span>
      <button data-id={base.id} onClick={retryBaseCleanup} disabled={busy}>继续清理</button></div>;
    return <button key={base.id} type="button" data-id={base.id} onClick={choose} disabled={!!upload || busy}
      className={base.id === active ? 'selected' : ''} aria-pressed={base.id === active}>{base.name}</button>;
  }

  /** 以实际阶段、计数和错误展示文档，不使用模拟整体百分比。 */
  function renderDocument(document: KnowledgeDocument) {
    const processing = document.status === 'processing';
    return <li key={document.id} className="knowledge-document">
      <div className="knowledge-document-heading"><strong>{document.filename}</strong><small>{(document.size / 1000).toFixed(1)} KB</small></div>
      <div role="status">{document.status === 'failed' ? '处理没有完成' : document.status === 'deleting' ? '正在清理' :
        document.status === 'ready' ? (document.warning ? '可以使用，但有内容未处理完成' : '可以使用') : stages[document.stage] || document.stage}
        {processing && document.total !== null && document.total > 0 && ` ${document.completed}/${document.total}`}</div>
      {processing && <progress aria-label={`${document.filename}处理进度`} max={document.total || undefined}
        value={document.total ? document.completed : undefined} />}
      {document.warning && <details className="knowledge-warning"><summary>部分处理没有完成</summary><p>基础搜索仍可使用。{document.warning}</p></details>}
      {document.error && <p role="alert">{document.error}</p>}
      <div className="knowledge-actions">
        {document.status !== 'deleting' && <a href={`/api/knowledge-bases/${active}/documents/${document.id}/file`} download>下载原件</a>}
        {(document.status === 'failed' || !!document.warning) && <button data-id={document.id} data-action="retry" onClick={documentAction} disabled={busy || processing}>重新处理</button>}
        <button data-id={document.id} data-action="delete" onClick={documentAction} disabled={busy}>{document.status === 'deleting' ? '继续清理' : '删除这份文档'}</button>
      </div>
    </li>;
  }

  return <section className="knowledge-page" aria-label="知识库管理">
    <header><h2>知识库</h2><p>把常用资料放在这里，对话时按需使用，不必每次重新上传。</p></header>
    {error && <p role="alert" className="knowledge-error">{error}</p>}
    {notice && <p role="status" className="knowledge-notice">{notice}</p>}
    <div className="knowledge-workspace">
      <aside className="knowledge-library-list"><h3>我的知识库</h3>
        <details className="knowledge-create-panel">
          <summary>＋ 新建知识库</summary>
          <form onSubmit={create}>
            <label>知识库名称<input aria-label="知识库名称" placeholder="例如：工作资料" maxLength={120} value={name} onChange={changeName} /></label>
            <button disabled={busy || !!upload || !name.trim()}>创建</button>
          </form>
        </details>
        {loading ? <p role="status">正在加载…</p> : bases.map(renderBase)}
      </aside>
      <div className="knowledge-documents">
        {active ? <>
          {editingName ? <form className="knowledge-title-form" onSubmit={saveName}>
            <label>知识库名称<input aria-label="当前知识库名称" maxLength={120} value={rename} onChange={changeRename} autoFocus /></label>
            <button disabled={busy || !!upload || !rename.trim()}>保存修改</button>
            <button type="button" className="secondary-action" onClick={cancelEditingName} disabled={busy || !!upload}>取消</button>
          </form> : <div className="knowledge-title-display">
            <div><span>当前知识库</span><h3>{rename || '未命名知识库'}</h3></div>
            <button type="button" className="secondary-action" onClick={startEditingName} disabled={busy || !!upload}>重命名</button>
            <details className="knowledge-more">
              <summary aria-label="知识库更多操作">⋯</summary>
              <div><button type="button" className="danger" onClick={deleteBase} disabled={busy || !!upload}>删除知识库</button></div>
            </details>
          </div>}
          <label className="knowledge-upload">添加文档<input type="file" multiple accept=".pdf,.docx,.txt,.md,.markdown" aria-label="上传知识库文档" onChange={uploadFiles} disabled={!!upload || busy} /></label>
          <p className="knowledge-help">支持 PDF、Word、TXT、Markdown · 最多 5 份，每份不超过 20 MB</p>
          {upload && <div className="knowledge-upload-status" role="status">正在上传{upload.total ? ` ${Math.round(upload.loaded * 100 / upload.total)}%` : '…'}
            <progress aria-label="文档上传进度" max={upload.total || undefined} value={upload.total ? upload.loaded : undefined} /></div>}
          <ul className="knowledge-document-list">{documents.map(renderDocument)}</ul>
          {!documents.length && !upload && <div className="knowledge-empty"><strong>这里还没有文档</strong><p>添加第一份资料后，可以在这里查看处理进度。</p></div>}
        </> : <div className="knowledge-empty"><strong>还没有知识库</strong><p>新建一个知识库，把常用资料集中放在这里。</p></div>}
      </div>
    </div>
  </section>;
}

/** 为单个会话保存已选知识库，切换会话后不会复用旧选择。 */
export function KnowledgeSelector({ conversationId, disabled }: { conversationId: string; disabled: boolean }) {
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const alive = useRef(true);
  useEffect(
    /** 同时加载账号知识库和服务端选择，离开会话后丢弃响应。 */
    function loadSelection() {
      alive.current = true;
      /** 将当前会话真实选择应用于复选框。 */
      async function load() {
        try {
          const [all, current] = await Promise.all([api<KnowledgeBase[]>('/knowledge-bases'), api<KnowledgeBase[]>(`/conversations/${conversationId}/knowledge-bases`)]);
          if (alive.current) { setBases(all.filter(
            /** 删除中的知识库只能清理，不能被新会话选择。 */
            (row) => !row.deleting,
          )); setSelected(current.map(
            /** 只保留服务端已授权的知识库身份。 */
            (row) => row.id,
          )); }
        } catch (failure) { if (alive.current) setError(errorMessage(failure)); }
      }
      void load();
      /** 组件以会话身份重建，卸载后不再应用选择响应。 */
      return () => { alive.current = false; };
    }, [conversationId],
  );
  /** 即时响应勾选并原子保存整组选择，失败回滚到上次服务端确认的值。 */
  async function toggle(event: ChangeEvent<HTMLInputElement>) {
    const id = event.target.value;
    const next = event.target.checked ? [...selected, id] : selected.filter(
      /** 取消选择只移除关系，不删除知识库资料。 */
      (item) => item !== id,
    );
    const previous = selected;
    setSelected(next); setSaving(true); setError('');
    try {
      const rows = await api<KnowledgeBase[]>(`/conversations/${conversationId}/knowledge-bases`, { method: 'PUT', body: JSON.stringify({ knowledge_base_ids: next }) });
      if (alive.current) setSelected(rows.map(
        /** 以服务端确认的实际选择更新界面。 */
        (row) => row.id,
      ));
    } catch (failure) { if (alive.current) { setSelected(previous); setError(errorMessage(failure)); } }
    finally { if (alive.current) setSaving(false); }
  }
  /** 为自己的每个知识库生成可访问的选择控件。 */
  function renderOption(base: KnowledgeBase) {
    return <label key={base.id}><input type="checkbox" value={base.id} checked={selected.includes(base.id)} onChange={toggle} disabled={disabled || saving} />{base.name}</label>;
  }
  return <details className="knowledge-selector"><summary>{saving ? '正在保存知识库选择…' : selected.length ? `本次对话使用 ${selected.length} 个知识库` : '本次对话未使用知识库'}</summary>
    <div>
      <p className="knowledge-selector-help">选择后，助手会按需读取这些知识库。</p>
      {bases.map(renderOption)}
      {!bases.length && <p>还没有知识库，可以先去“知识库”添加资料。</p>}
      {error && <p role="alert">{error}</p>}
    </div>
  </details>;
}
