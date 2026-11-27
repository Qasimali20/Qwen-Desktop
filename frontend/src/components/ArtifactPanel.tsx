import { useEffect, useMemo, useRef, useState } from 'react'
import { Code2, Download, Eye, RotateCw, X } from 'lucide-react'
import { useApp } from '../store/app'
import { CopyButton, Markdown } from './Markdown'

const EXT: Record<string, string> = { html: 'html', svg: 'svg', mermaid: 'mmd', markdown: 'md', md: 'md' }

function srcDoc(lang: string, code: string): string {
  if (lang === 'svg') {
    return `<!doctype html><html><body style="margin:0;display:grid;place-items:center;min-height:100vh;background:#fff">${code}</body></html>`
  }
  if (lang === 'mermaid') {
    const escaped = code.replace(/&/g, '&amp;').replace(/</g, '&lt;')
    return `<!doctype html><html><body style="margin:0;padding:24px;background:#fff">
<pre class="mermaid">${escaped}</pre>
<script type="module">
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs';
mermaid.initialize({ startOnLoad: true, theme: 'default', securityLevel: 'strict' });
</script></body></html>`
  }
  return code
}

export function ArtifactPanel() {
  const artifact = useApp((s) => s.artifact)
  const close = () => useApp.getState().openArtifact(null)
  const [tab, setTab] = useState<'preview' | 'code'>('preview')
  const [nonce, setNonce] = useState(0)
  const [width, setWidth] = useState(() => Math.round(window.innerWidth * 0.45))
  const dragging = useRef(false)

  useEffect(() => setTab('preview'), [artifact?.key])

  useEffect(() => {
    const move = (e: PointerEvent) => {
      if (dragging.current) setWidth(Math.min(Math.max(window.innerWidth - e.clientX, 360), window.innerWidth - 420))
    }
    const up = () => { dragging.current = false; document.body.classList.remove('resizing') }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    return () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up) }
  }, [])

  // Re-rendering the iframe on every streamed token would flicker: debounce preview updates.
  const [doc, setDoc] = useState('')
  useEffect(() => {
    if (!artifact) return
    const t = setTimeout(() => setDoc(srcDoc(artifact.lang, artifact.code)), 300)
    return () => clearTimeout(t)
  }, [artifact])

  const isDoc = artifact && (artifact.lang === 'markdown' || artifact.lang === 'md')
  const download = useMemo(() => {
    if (!artifact) return null
    return () => {
      const blob = new Blob([artifact.code], { type: 'text/plain' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `${artifact.title.replace(/[^\w-]+/g, '_').slice(0, 60) || 'artifact'}.${EXT[artifact.lang] ?? 'txt'}`
      a.click()
      URL.revokeObjectURL(a.href)
    }
  }, [artifact])

  if (!artifact) return null
  return (
    <aside className="artifact-panel" style={{ width }}>
      <div className="resize-handle" onPointerDown={() => { dragging.current = true; document.body.classList.add('resizing') }} />
      <header className="artifact-header">
        <div className="seg">
          <button className={tab === 'preview' ? 'on' : ''} onClick={() => setTab('preview')}><Eye size={14} /> Preview</button>
          <button className={tab === 'code' ? 'on' : ''} onClick={() => setTab('code')}><Code2 size={14} /> Code</button>
        </div>
        <span className="artifact-name" title={artifact.title}>{artifact.title}</span>
        <div className="artifact-actions">
          {tab === 'preview' && !isDoc && <button className="icon-btn sm" title="Reload" onClick={() => setNonce((n) => n + 1)}><RotateCw size={14} /></button>}
          <CopyButton text={artifact.code} />
          <button className="icon-btn sm" title="Download" onClick={download!}><Download size={14} /></button>
          <button className="icon-btn sm" title="Close" onClick={close}><X size={16} /></button>
        </div>
      </header>
      <div className="artifact-body">
        {tab === 'code' ? (
          <Markdown text={`\`\`\`${artifact.lang === 'md' ? 'markdown' : artifact.lang}\n${artifact.code}\n\`\`\``} />
        ) : isDoc ? (
          <div className="artifact-doc"><Markdown text={artifact.code} /></div>
        ) : (
          <iframe
            key={`${artifact.key}:${nonce}`}
            title={artifact.title}
            srcDoc={doc}
            sandbox="allow-scripts allow-modals allow-forms allow-popups allow-downloads"
          />
        )}
      </div>
    </aside>
  )
}
