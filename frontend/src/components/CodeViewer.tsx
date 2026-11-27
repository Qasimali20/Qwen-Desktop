import { useEffect, useRef, useState } from 'react'
import { Check, Copy, FileCode2, Loader2, X } from 'lucide-react'
import { api } from '../api/client'
import type { RepoFile } from '../api/types'
import { useApp, type CodeView } from '../store/app'

const close = () => useApp.getState().openCode(null)

/** Read-only source view opened from a code citation; the cited lines are highlighted and scrolled to. */
export function CodeViewer() {
  const view = useApp((s) => s.codeView)
  // Remount per file so state starts fresh without resetting it inside an effect.
  return view ? <Viewer key={`${view.repoId}:${view.path}:${view.start ?? ''}`} view={view} /> : null
}

function Viewer({ view }: { view: CodeView }) {
  const [file, setFile] = useState<RepoFile | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const target = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let live = true
    api.repoFile(view.repoId, view.path).then((f) => live && setFile(f)).catch((e) => live && setError((e as Error).message))
    return () => { live = false }
  }, [view.repoId, view.path])

  useEffect(() => { target.current?.scrollIntoView({ block: 'center' }) }, [file])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && close()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const start = view.start ?? 0
  const end = view.end ?? start
  const lines = file?.text.split('\n') ?? []
  const width = String(lines.length).length

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && close()}>
      <div className="modal code-viewer" role="dialog" aria-label={`Source: ${view.path}`}>
        <header>
          <div className="cv-title">
            <FileCode2 size={16} />
            <span className="cv-path" title={file ? `${file.repo}/${file.path}` : view.path}>{file ? `${file.repo}/${file.path}` : view.path}</span>
            {file && <span className="muted small">{file.lines} lines{file.commit_sha ? ` · ${file.commit_sha.slice(0, 7)}` : ''}{start ? ` · lines ${start}–${end}` : ''}</span>}
          </div>
          <div className="cv-actions">
            {file && (
              <button className="icon-btn sm" title="Copy file" onClick={() => {
                void navigator.clipboard.writeText(file.text)
                setCopied(true)
                setTimeout(() => setCopied(false), 1200)
              }}>{copied ? <Check size={14} /> : <Copy size={14} />}</button>
            )}
            <button className="icon-btn sm" title="Close" onClick={close}><X size={16} /></button>
          </div>
        </header>
        <div className="cv-body">
          {error ? <p className="form-error">{error}</p> : !file ? <Loader2 className="spin" /> : (
            <pre className="cv-code">
              {lines.map((l, i) => {
                const n = i + 1
                const hit = start > 0 && n >= start && n <= end
                return (
                  <div key={n} ref={n === start ? target : undefined} className={`cv-line${hit ? ' hit' : ''}`}>
                    <span className="cv-n">{String(n).padStart(width)}</span>{l || ' '}
                  </div>
                )
              })}
            </pre>
          )}
        </div>
      </div>
    </div>
  )
}
