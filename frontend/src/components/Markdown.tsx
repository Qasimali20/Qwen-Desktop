import { memo, useEffect, useMemo, useState, type ReactNode } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import { Check, Copy, Download, Eye, FileCode2 } from 'lucide-react'
import { useApp } from '../store/app'
import { ARTIFACT_LANGS } from '../lib/text'
import { codeFileName, downloadText, isPatch } from '../lib/codeFile'

type HastNode = {
  type: string; value?: string; children?: HastNode[]; position?: { start: { offset?: number } }
  data?: { meta?: string }
}

function hastText(n: HastNode): string {
  if (n.type === 'text') return n.value ?? ''
  return (n.children ?? []).map(hastText).join('')
}

export function CopyButton({ text, label }: { text: string; label?: string }) {
  const [done, setDone] = useState(false)
  return (
    <button
      className="icon-btn sm"
      title="Copy"
      onClick={() => {
        void navigator.clipboard.writeText(text)
        setDone(true)
        setTimeout(() => setDone(false), 1500)
      }}
    >
      {done ? <Check size={14} /> : <Copy size={14} />}
      {label && <span>{done ? 'Copied' : label}</span>}
    </button>
  )
}

const MIN_MARKDOWN_ARTIFACT_LINES = 40

function artifactTitle(lang: string, code: string): string {
  const t = code.match(/<title>([^<]{1,80})<\/title>/i)?.[1] ?? code.match(/^#\s+(.{1,80})$/m)?.[1]
  if (t) return t.trim()
  return { html: 'Web page', svg: 'SVG graphic', mermaid: 'Diagram', markdown: 'Document', md: 'Document' }[lang] ?? 'Artifact'
}

function ArtifactCard({ id, lang, code, live }: { id: string; lang: string; code: string; live: boolean }) {
  const { artifact, openArtifact } = useApp()
  const title = artifactTitle(lang, code)
  const isOpen = artifact?.key === id

  // While streaming, open the panel and keep it in sync with the growing code.
  useEffect(() => {
    if (!live) return
    const cur = useApp.getState().artifact
    if (!cur || cur.key === id) openArtifact({ key: id, title, lang, code })
  }, [live, id, title, lang, code, openArtifact])

  // Once finished, refresh the panel if it is showing this artifact.
  useEffect(() => {
    if (!live && useApp.getState().artifact?.key === id) openArtifact({ key: id, title, lang, code })
  }, [live, id, title, lang, code, openArtifact])

  return (
    <button className={`artifact-card${isOpen ? ' active' : ''}`}
      onClick={() => openArtifact(isOpen ? null : { key: id, title, lang, code })}>
      <span className="artifact-icon"><FileCode2 size={20} /></span>
      <span className="artifact-meta">
        <span className="artifact-title">{title}</span>
        <span className="artifact-sub">
          {live ? 'Generating…' : `${lang.toUpperCase()} · ${code.split('\n').length} lines · click to ${isOpen ? 'close' : 'open'}`}
        </span>
      </span>
      <Eye size={16} className="artifact-eye" />
    </button>
  )
}

function CodeBlock({ lang, code, meta, children }: { lang: string; code: string; meta?: string; children: ReactNode }) {
  const filename = codeFileName(lang, code, meta)
  const patch = isPatch(lang, code)
  const named = !filename.startsWith('snippet.')
  return (
    <div className="code-block">
      <div className="code-head">
        <span className="code-lang" title={named ? filename : undefined}>{named ? filename : lang || 'text'}</span>
        <span className="code-actions">
          <button className="icon-btn sm" title={patch ? `Download ${filename} — apply with: git apply ${filename}` : `Download ${filename}`}
            onClick={() => downloadText(filename, code)}>
            <Download size={14} /><span>{patch ? 'Patch' : 'Download'}</span>
          </button>
          <CopyButton text={code} label="Copy" />
        </span>
      </div>
      <pre><code className={`hljs language-${lang}`}>{children}</code></pre>
    </div>
  )
}

interface Props {
  text: string
  /** Prefix for artifact keys; artifacts are only extracted when set. */
  artifactKey?: string
  streaming?: boolean
}

function MarkdownImpl({ text, artifactKey, streaming = false }: Props) {
  const components = useMemo<Components>(() => ({
    pre: ({ children }) => <>{children}</>,
    code: ({ node, className, children }) => {
      const lang = /language-([\w-]+)/.exec(className ?? '')?.[1]?.toLowerCase() ?? ''
      const n = node as unknown as HastNode
      const code = hastText(n).replace(/\n$/, '')
      const isBlock = !!lang || code.includes('\n')
      if (!isBlock) return <code className="inline-code">{children}</code>
      const lines = code.split('\n').length
      const artifact = artifactKey && ARTIFACT_LANGS.has(lang)
        && (lang === 'html' || lang === 'svg' || lang === 'mermaid' || lines >= MIN_MARKDOWN_ARTIFACT_LINES)
      if (artifact) {
        const offset = n.position?.start.offset ?? 0
        // The block is "live" while streaming and its closing fence has not arrived yet.
        const closed = !streaming || /^\s*```\s*$/m.test(text.slice(text.indexOf('\n', offset) + 1))
        return <ArtifactCard id={`${artifactKey}:${offset}`} lang={lang} code={code} live={streaming && !closed} />
      }
      return <CodeBlock lang={lang} code={code} meta={n.data?.meta}>{children}</CodeBlock>
    },
    a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>,
    table: ({ children }) => <div className="table-wrap"><table>{children}</table></div>,
  }), [artifactKey, streaming, text])

  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: true }]]}
        rehypePlugins={[[rehypeHighlight, { detect: false, ignoreMissing: true }], rehypeKatex]}
        components={components}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}

export const Markdown = memo(MarkdownImpl)
