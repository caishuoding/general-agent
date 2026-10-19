import { useEffect, useRef, useState, type ReactNode, type MouseEvent } from "react";
import Markdown, { type Components, type ExtraProps } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { visit, SKIP } from "unist-util-visit";
import type { Root, PhrasingContent } from "mdast";
import type { Message } from "./api";
import "./answer.css";

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
function sourceId(messageId: string, key: string) {
  return `answer-source-${encodeURIComponent(messageId)}-${encodeURIComponent(key)}`;
}

/** 根据服务端网页来源编号创建 Markdown 插件，仅转换正文中的真实网页引用。 */
function citationPlugin(message: Message) {
  const keys = new Set((message.web_sources || []).map(function getKey(source) { return source.key; }));
  /** 返回语法树转换器，不修改代码、已有链接或未知来源标记。 */
  return function citations() {
    /** 将普通文本中的引用拆为文字和链接，保留原有 Markdown 结构。 */
    return function transform(tree: Root) {
      visit(tree, function replaceCitation(node, index, parent) {
        if (node.type === "link" || node.type === "linkReference") return SKIP;
        if (node.type !== "text" || index === undefined || !parent) return;
        const parts: PhrasingContent[] = [];
        let cursor = 0;
        for (const match of node.value.matchAll(/\[(W\d+)\]/g)) {
          if (!keys.has(match[1])) continue;
          const start = match.index;
          if (start > cursor) parts.push({ type: "text", value: node.value.slice(cursor, start) });
          parts.push({ type: "link", url: `#${sourceId(message.id, match[1])}`, children: [{ type: "text", value: match[0] }] });
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

const components: Components = {
  pre: CodeBlock,
  /** 为宽表格提供独立滚动区域，不让表格撑开整条回答。 */
  table({ node: _node, children, ...props }) {
    return <div className="answer-table" tabIndex={0} role="region" aria-label="回答表格"><table {...props}>{children}</table></div>;
  },
  /** 安全链接在新页打开；来源链接则展开本条回答中对应的原文摘录。 */
  a({ node: _node, href, children, ...props }) {
    if (!href) return <span>{children}</span>;
    if (href.startsWith("#answer-source-")) {
      /** 阻止页面导航，展开来源和父级折叠容器后定位并聚焦来源。 */
      function revealSource(event: MouseEvent<HTMLAnchorElement>) {
        event.preventDefault();
        const target = document.getElementById(href!.slice(1));
        if (!target) return;
        let ancestor: HTMLElement | null = target;
        while (ancestor) {
          if (ancestor instanceof HTMLDetailsElement) ancestor.open = true;
          ancestor = ancestor.parentElement;
        }
        target.scrollIntoView({ block: "center" });
        target.focus({ preventScroll: true });
      }
      return <a {...props} className="answer-citation" href={href} onClick={revealSource}>{children}</a>;
    }
    return <a {...props} href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
  },
  /** 外部图片仅显示链接，避免模型输出触发自动远程加载。 */
  img({ src, alt }) {
    return src ? <a href={src} target="_blank" rel="noopener noreferrer">{alt || "查看图片"}</a> : <span>{alt}</span>;
  },
};

/** 接收助手消息及所属会话，输出安全 Markdown、复制入口与可定位的真实来源。 */
export function AnswerContent({ message }: {
  message: Message;
}) {
  const webSources = message.web_sources || [];
  const sourceCount = webSources.length;
  return <>
    <div className="message-content answer-markdown">
      <Markdown skipHtml remarkPlugins={[remarkGfm, citationPlugin(message)]} rehypePlugins={[[rehypeHighlight, { detect: false }]]} components={components}>{message.content}</Markdown>
    </div>
    {sourceCount > 0 && <section className="answer-sources" aria-label="回答来源">
      <div className="answer-sources-heading">参考来源 · {sourceCount}</div>
      {webSources.map(function renderWebSource(source) {
        /** 输出网页来源详情并绑定本回答唯一定位锚点。 */
        return <SourceDetails key={source.key} id={sourceId(message.id, source.key)} source={source} />;
      })}
    </section>}
    {!!message.content && <div className="answer-actions"><CopyButton text={message.content} label="复制回答" /></div>}
  </>;
}
