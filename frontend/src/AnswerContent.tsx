import { Children, isValidElement, useEffect, useRef, useState, type ReactNode, type MouseEvent } from "react";
import Markdown, { type Components, type ExtraProps } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { visit, SKIP } from "unist-util-visit";
import type { Root, PhrasingContent } from "mdast";
import type { Message } from "./api";
import "./answer.css";

type KnowledgeSource = Message["knowledge_sources"][number];
type KnowledgeTable = NonNullable<KnowledgeSource["table"]>;
type KnowledgeTableCell = KnowledgeTable["cells"][number];

/** 将普通段落中的单个换行转换为可见换行，避免粗体小标题与正文粘连。 */
function lineBreakPlugin() {
  /** 返回只处理普通段落文本节点的语法树转换器。 */
  return function lineBreaks() {
    /** 拆分正文中的软换行，不修改代码、表格或链接结构。 */
    return function transform(tree: Root) {
      visit(tree, function replaceLineBreaks(node, index, parent) {
        if (node.type !== 'text' || index === undefined || !parent || parent.type !== 'paragraph'
            || !node.value.includes('\n')) return;
        const parts: PhrasingContent[] = [];
        node.value.split('\n').forEach(function appendLine(line, position, lines) {
          if (line) parts.push({ type: 'text', value: line });
          if (position < lines.length - 1) parts.push({ type: 'break' });
        });
        parent.children.splice(index, 1, ...parts);
        return index + parts.length;
      });
    };
  };
}

/** 根据文本和按钮名称提供复制操作；成功、失败均返回可见反馈。 */
function CopyButton({ text, label }: { text: string; label: string }) {
  const [feedback, setFeedback] = useState("");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(function cleanUpCopyFeedback() {
    /** 卸载时清除反馈定时器，避免继续更新已离开的回答。 */
    /** 清除定时器，避免卸载后继续更新反馈状态。 */
    return function disposeTimer() {
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  /** 将原始文本写入系统剪贴板；权限或环境不支持时显示失败提示。 */
  async function copy() {
    if (timer.current) clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(text);
      setFeedback("已复制");
    } catch {
      setFeedback("复制失败，请手动选择复制");
    }
    timer.current = setTimeout(function clearFeedback() { setFeedback(""); }, 2500);
  }

  return <span className="copy-action">
    <button type="button" onClick={copy} aria-label={label}>{label}</button>
    <span role="status">{feedback}</span>
  </span>;
}

/** 从高亮语法树提取原文；递归拼接文本，确保复制不包含高亮标签。 */
function codeText(node: NonNullable<ExtraProps["node"]>): string {
  return node.children.map(function readChild(child): string {
    if (child.type === "text") return child.value;
    return child.type === "element" ? codeText(child) : "";
  }).join("");
}

/** 将代码块节点与高亮内容显示为独立容器，输出语言标记和原文复制入口。 */
function CodeBlock({ node, children }: ExtraProps & { children?: ReactNode }) {
  const code = node?.children.find(function findCode(child) { return child.type === "element" && child.tagName === "code"; });
  const classes = code?.type === "element" ? String(code.properties.className || "") : "";
  const language = /language-([\w+-]+)/.exec(classes)?.[1] || "纯文本";
  return <div className="answer-code">
    <div className="answer-code-toolbar"><span>{language}</span><CopyButton text={node ? codeText(node) : ""} label="复制代码" /></div>
    <pre tabIndex={0} aria-label={`${language}代码`}>{children}</pre>
  </div>;
}

/** 为本条消息的可信来源生成独立锚点，防止不同回答的相同编号发生冲突。 */
export function sourceId(messageId: string, key: string) {
  return `answer-source-${encodeURIComponent(messageId)}-${encodeURIComponent(key)}`;
}

/** 定位到已展开的可信来源，并为键盘用户保留焦点。 */
function scrollToSource(identifier: string) {
  const target = document.getElementById(identifier);
  if (!target) return;
  let ancestor: HTMLElement | null = target;
  while (ancestor) {
    if (ancestor instanceof HTMLDetailsElement) ancestor.open = true;
    ancestor = ancestor.parentElement;
  }
  target.scrollIntoView({ block: 'center' });
  target.focus({ preventScroll: true });
}

type RenderedMessage = Pick<Message, 'id' | 'content' | 'web_sources' | 'knowledge_sources'>;
const STREAM_RESUMED_TOKEN = /\[\[STREAM_RESUMED(?::\d+)?\]\]/;
const STREAM_RESUMED_SPLIT = /(\[\[STREAM_RESUMED(?::\d+)?\]\])/;

/** 移除仅用于界面分段的恢复标记，得到适合复制和后续阅读的正文。 */
function withoutStreamMarkers(content: string) {
  return content
    .split(STREAM_RESUMED_SPLIT)
    .filter(function keepText(part) { return !STREAM_RESUMED_TOKEN.test(part); })
    .join("")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** 在真实断点处显示持久提示，说明正文经过自动恢复和续写。 */
function StreamResumeMarker() {
  return <div className="stream-resume-marker" role="status">
    <span aria-hidden="true">↻</span>
    <span>连接中断</span>
    <strong>已自动恢复并继续</strong>
  </div>;
}

/** 根据服务端来源编号创建 Markdown 插件，仅转换正文中真实存在的网页或知识库引用。 */
function citationPlugin(message: RenderedMessage, citationNumbers: Map<string, string>) {
  const webKeys = new Set((message.web_sources || []).map(function getKey(source) { return source.key; }));
  const knowledge = new Map((message.knowledge_sources || []).map(function indexSource(source) { return [source.key, source]; }));
  /** 返回语法树转换器，不修改代码、已有链接或未知来源标记。 */
  return function citations() {
    /** 将普通文本中的引用拆为文字和链接，保留原有 Markdown 结构。 */
    return function transform(tree: Root) {
      visit(tree, function replaceCitation(node, index, parent) {
        if (node.type === "link" || node.type === "linkReference") return SKIP;
        if (node.type !== "text" || index === undefined || !parent) return;
        const parts: PhrasingContent[] = [];
        let cursor = 0;
        for (const match of node.value.matchAll(/\[\[MEDIA:(K\d+)\]\]|\[([WK]\d+)\]/g)) {
          const mediaKey = match[1];
          const key = mediaKey || match[2];
          const knowledgeSource = knowledge.get(key);
          if (!webKeys.has(key) && !knowledgeSource) continue;
          const start = match.index;
          if (start > cursor) parts.push({ type: "text", value: node.value.slice(cursor, start) });
          if (mediaKey && knowledgeSource && knowledgeSourceKind(knowledgeSource) !== 'text') {
            parts.push({ type: "link", url: `#media-${knowledgeSource.key}`, children: [{ type: "text", value: match[0] }] });
            cursor = start + match[0].length;
            continue;
          }
          const label = knowledgeSource
            ? `[${citationNumbers.get(key) || '?'}]`
            : match[0];
          parts.push({ type: "link", url: `#${sourceId(message.id, key)}`, children: [{ type: "text", value: label }] });
          cursor = start + match[0].length;
        }
        if (!parts.length) return;
        if (cursor < node.value.length) parts.push({ type: "text", value: node.value.slice(cursor) });
        parent.children.splice(index, 1, ...parts);
        return index + parts.length;
      });
    };
  };
}

/** 根据知识库来源和认证入口根路径返回受保护的原件下载地址。 */
function knowledgeDocumentUrl(source: Message["knowledge_sources"][number], resourceRoot = '/api') {
  const base = `${resourceRoot}/knowledge-bases/${encodeURIComponent(source.knowledge_base_id)}/documents/${encodeURIComponent(source.document_id)}/file`;
  return source.filename.toLowerCase().endsWith('.pdf') && source.page ? `${base}#page=${source.page}` : base;
}

/** 根据知识库来源和认证入口根路径返回原图的受保护内联地址。 */
function knowledgeImageUrl(source: Message["knowledge_sources"][number], resourceRoot = '/api') {
  return source.image_id
    ? `${resourceRoot}/knowledge-bases/${encodeURIComponent(source.knowledge_base_id)}/documents/${encodeURIComponent(source.document_id)}/images/${encodeURIComponent(source.image_id)}/file`
    : null;
}

/** 统一判断知识库来源应展示为图片、表格还是普通文字。 */
function knowledgeSourceKind(source: KnowledgeSource): 'image' | 'table' | 'text' {
  if (source.kind === 'table' || source.table) return 'table';
  if (source.kind === 'image' || source.image_id) return 'image';
  if (source.location.startsWith('表格') || source.excerpt.trimStart().startsWith('|')) return 'table';
  return 'text';
}

/** 按单元格零起始行列组装表格，保留跨行跨列且不复制合并格内容。 */
function tableCellRows(table: KnowledgeTable): KnowledgeTableCell[][] {
  /** 创建每个逻辑行的空单元格列表。 */
  const rows = Array.from({ length: table.rows }, function emptyRow() { return [] as KnowledgeTableCell[]; });
  const occupied = new Set<string>();
  /** 放置单元格并标记其跨行跨列覆盖的网格位置。 */
  table.cells.forEach(function placeCell(cell) {
    if (cell.row < 0 || cell.row >= rows.length || occupied.has(`${cell.row}:${cell.column}`)) return;
    rows[cell.row].push(cell);
    for (let row = cell.row; row < Math.min(table.rows, cell.row + (cell.rowspan ?? 1)); row += 1) {
      for (let column = cell.column; column < cell.column + (cell.colspan ?? 1); column += 1) {
        occupied.add(`${row}:${column}`);
      }
    }
  });
  /** 按原始列坐标稳定排列同一行的单元格。 */
  rows.forEach(function sortColumns(row) { row.sort(function byColumn(left, right) { return left.column - right.column; }); });
  return rows;
}

/** 使用服务端已核验的单元格结构渲染可合并的可视化表格，避免依赖模型生成的 HTML。 */
function StructuredKnowledgeTable({ table }: { table: KnowledgeTable }) {
  const rows = tableCellRows(table);
  /** 返回保留真实跨行跨列关系的只读表格。 */
  return <div className="answer-table structured-source-table" tabIndex={0} role="region" aria-label="知识库表格">
    <table>
      {table.title && <caption>{table.title}</caption>}
      <tbody>
        {rows.map(function renderRow(cells, rowIndex) {
          return <tr key={rowIndex}>
            {cells.map(function renderCell(cell) {
              /** 将单元格文字中的真实换行保留为安全文本节点。 */
              const content = cell.text.split('\n').map(function renderLine(line, index) {
                return <span key={index}>{index > 0 && <br />}{line}</span>;
              });
              return cell.header
                ? <th key={`${cell.row}-${cell.column}`} rowSpan={cell.rowspan ?? 1} colSpan={cell.colspan ?? 1} scope={(cell.colspan ?? 1) === 1 ? 'col' : 'colgroup'}>{content}</th>
                : <td key={`${cell.row}-${cell.column}`} rowSpan={cell.rowspan ?? 1} colSpan={cell.colspan ?? 1}>{content}</td>;
            })}
          </tr>;
        })}
      </tbody>
    </table>
  </div>;
}

/** 将一个可信网页来源渲染为可展开摘录。 */
function SourceDetails({ id, source }: {
  id: string;
  source: Message["web_sources"][number];
}) {
  const href = source.url;
  const label = source.title || source.url;
  /** 返回可折叠来源详情，用户可展开摘录核对回答依据。 */
  return <details className="file-citation" id={id} tabIndex={-1}>
    <summary>[{source.key}] <a href={href} target="_blank" rel="noopener noreferrer">{label}</a></summary>
    <blockquote>{source.excerpt}</blockquote>
  </details>;
}

/** 输出只含文件与原文位置的紧凑参考来源链接。 */
function KnowledgeSourceLink({ id, source, number, resourceRoot }: { id: string; source: KnowledgeSource; number: string; resourceRoot: string }) {
  const label = source.filename || '知识库文档';
  const kind = knowledgeSourceKind(source);
  /** 返回可定位原文位置的简单来源条目。 */
  return <div className="knowledge-source-link" id={id} tabIndex={-1}>
    <div className="knowledge-source-summary">
      <span>[{number}]</span>
      <a href={knowledgeDocumentUrl(source, resourceRoot)} target="_blank" rel="noopener noreferrer">{label}</a>
      <small className="knowledge-source-location">{source.location}</small>
    </div>
    {kind === 'text' && <p className="knowledge-source-excerpt" title={source.excerpt}>{source.excerpt}</p>}
  </div>;
}

/** 在模型指定段落之后嵌入真实召回的图片或表格。 */
function InlineKnowledgeMedia({ source, resourceRoot }: { source: KnowledgeSource; resourceRoot: string }) {
  const label = source.filename || '知识库文档';
  const image = knowledgeImageUrl(source, resourceRoot);
  const kind = knowledgeSourceKind(source);
  /** 返回与正文融为一体的行内媒体，来源由底部参考列表统一提供。 */
  return <figure className="inline-knowledge-media" data-media={source.key}>
    {kind === 'image' && image && <a href={image} target="_blank" rel="noopener noreferrer">
      <img src={image} alt={`${label} · ${source.location}`} loading="lazy" />
    </a>}
    {kind === 'table' && <div className="knowledge-source-table">
      {source.table?.render_as_image && image
        ? <a href={image} target="_blank" rel="noopener noreferrer"><img src={image} alt={`${label} · ${source.location}`} loading="lazy" /></a>
        : source.table ? <StructuredKnowledgeTable table={source.table} />
          : <Markdown skipHtml remarkPlugins={[remarkGfm]} components={components}>{source.excerpt}</Markdown>}
    </div>}
  </figure>;
}

const components: Components = {
  pre: CodeBlock,
  /** 为宽表格提供独立滚动区域，不让表格撑开整条回答。 */
  table({ node: _node, children, ...props }) {
    return <div className="answer-table" tabIndex={0} role="region" aria-label="回答表格"><table {...props}>{children}</table></div>;
  },
  /** 普通 Markdown 链接在新页打开。 */
  a({ node: _node, href, children, ...props }) {
    if (!href) return <span>{children}</span>;
    return <a {...props} href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
  },
  /** 外部图片仅显示链接，避免模型输出触发自动远程加载。 */
  img({ src, alt }) {
    return src ? <a href={src} target="_blank" rel="noopener noreferrer">{alt || "查看图片"}</a> : <span>{alt}</span>;
  },
};

/** 为当前回答提供引用定位、行内媒体和普通 Markdown 的组合渲染器。 */
function answerComponents(message: RenderedMessage, onCitationClick: (identifier: string) => void, resourceRoot: string): Components {
  const knowledge = new Map((message.knowledge_sources || []).map(function indexSource(source) { return [source.key, source]; }));
  return {
    ...components,
    /** 来源锚点原位展开；媒体标记交给行内媒体组件，普通链接保持安全跳转。 */
    a({ node: _node, href, children, ...props }) {
      if (!href) return <span>{children}</span>;
      if (href.startsWith('#media-')) {
        const source = knowledge.get(href.slice('#media-'.length));
        return source ? <InlineKnowledgeMedia source={source} resourceRoot={resourceRoot} /> : <span>{children}</span>;
      }
      if (href.startsWith('#answer-source-')) {
        const identifier = href.slice(1);
        /** 由回答容器先展开来源区，再定位到真实来源条目。 */
        function reveal(event: MouseEvent<HTMLAnchorElement>) {
          event.preventDefault();
          onCitationClick(identifier);
        }
        return <a {...props} className="answer-citation" href={href} onClick={reveal}>{children}</a>;
      }
      return <a {...props} href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
    },
    /** 把混在文字段落中的媒体引用移到整段之后，确保图片和表格保持合法块级布局。 */
    p({ node: _node, children, ...props }) {
      const values = Children.toArray(children);
      const media = values.filter(function isMedia(child) {
        return isValidElement(child) && child.type === InlineKnowledgeMedia;
      });
      if (!media.length) return <p {...props}>{children}</p>;
      const text = values.filter(function isText(child) { return !media.includes(child); });
      const meaningful = text.some(function hasText(child) {
        return typeof child === 'string' ? child.trim().length > 0 : true;
      });
      return <div className="answer-paragraph-with-media">
        {meaningful && <p>{text}</p>}
        {media}
      </div>;
    },
  };
}

/** 使用消息与认证入口根路径渲染安全 Markdown，输出可定位的真实引用。 */
export function AnswerMarkdown({ message, onCitationClick, className = '', resourceRoot = '/api' }: {
  message: RenderedMessage;
  onCitationClick?: (identifier: string) => void;
  className?: string;
  resourceRoot?: string;
}) {
  const citationNumbers = new Map((message.knowledge_sources || []).map(function numberSource(source, index) {
    // 为知识库来源分配面向用户的连续编号。
    return [source.key, String(index + 1)];
  }));
  /** 没有外部定位回调时保留正文锚点，不额外展开页面状态。 */
  function reveal(identifier: string) {
    if (onCitationClick) onCitationClick(identifier);
    else scrollToSource(identifier);
  }
  const pieces = message.content.split(STREAM_RESUMED_SPLIT);
  return <div className={`message-content answer-markdown ${className}`.trim()}>
    {pieces.map(function renderPiece(piece, index) {
      // 恢复标记不交给 Markdown 解析，确保断点位置稳定且不会被复制为内部字符串。
      if (STREAM_RESUMED_TOKEN.test(piece)) return <StreamResumeMarker key={`resume-${index}`} />;
      return piece ? <Markdown
        key={`content-${index}`}
        skipHtml
        remarkPlugins={[remarkGfm, lineBreakPlugin(), citationPlugin(message, citationNumbers)]}
        rehypePlugins={[[rehypeHighlight, { detect: false }]]}
        components={answerComponents(message, reveal, resourceRoot)}
      >{piece}</Markdown> : null;
    })}
  </div>;
}

/** 接收助手消息和认证入口根路径，输出安全 Markdown、复制入口与可定位的真实来源。 */
export function AnswerContent({ message, resourceRoot = '/api' }: {
  message: Message;
  resourceRoot?: string;
}) {
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const pendingSource = useRef<string | null>(null);
  const webSources = message.web_sources || [];
  const knowledgeSources = message.knowledge_sources || [];
  const sourceCount = webSources.length + knowledgeSources.length;
  const sourceSectionId = `answer-sources-${encodeURIComponent(message.id)}`;
  const citationNumbers = new Map(knowledgeSources.map(function numberSource(source, index) {
    // 为知识库来源分配面向用户的连续编号。
    return [source.key, String(index + 1)];
  }));

  // 来源区首次展开后定位正文所点击的引用。
  useEffect(function revealPendingSource() {
    /** 来源区渲染完成后再滚动，确保首次点击引用也能定位。 */
    if (!sourcesOpen || !pendingSource.current) return;
    const identifier = pendingSource.current;
    pendingSource.current = null;
    // 等来源区完成布局后再滚动，避免定位到旧位置。
    requestAnimationFrame(function scrollAfterPaint() { scrollToSource(identifier); });
  }, [sourcesOpen]);

  /** 展开来源区并定位正文引用；已展开时直接滚动到目标。 */
  function revealSource(identifier: string) {
    if (sourcesOpen) {
      // 已展开时无需等待状态更新，下一帧直接定位。
      requestAnimationFrame(function scrollOpenSource() { scrollToSource(identifier); });
      return;
    }
    pendingSource.current = identifier;
    setSourcesOpen(true);
  }

  /** 切换参考来源区的展开状态，默认保持收起以减少正文干扰。 */
  function toggleSources() {
    /** 反转当前参考来源区的展开状态。 */
    function invert(open: boolean) { return !open; }
    setSourcesOpen(invert);
  }

  return <>
    <AnswerMarkdown message={message} onCitationClick={revealSource} resourceRoot={resourceRoot} />
    {sourceCount > 0 && <div className="answer-source-toggle">
      <button type="button" aria-expanded={sourcesOpen} aria-controls={sourceSectionId} onClick={toggleSources}>
        参考来源 · {sourceCount}
      </button>
      {sourcesOpen && <section className="answer-sources" id={sourceSectionId} aria-label="回答来源">
        {webSources.map(function renderWebSource(source) {
          /** 输出网页来源详情并绑定本回答唯一定位锚点。 */
          return <SourceDetails key={source.key} id={sourceId(message.id, source.key)} source={source} />;
        })}
        {knowledgeSources.map(function renderKnowledgeSource(source) {
          /** 知识库来源只展示文件、位置和定位链接，不重复切片正文。 */
          return <KnowledgeSourceLink key={source.key} id={sourceId(message.id, source.key)} source={source} resourceRoot={resourceRoot}
            number={citationNumbers.get(source.key) || '?'} />;
        })}
      </section>}
    </div>}
    {!!message.content && <div className="answer-actions"><CopyButton text={withoutStreamMarkers(message.content)} label="复制回答" /></div>}
  </>;
}
