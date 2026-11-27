import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, FileArchive, GitBranch, Loader2, Plus, RefreshCw, Trash2, Upload } from 'lucide-react'
import { api } from '../api/client'
import type { Repo } from '../api/types'
import { formatBytes, relativeDay, timeAgo } from '../lib/text'
import { useApp } from '../store/app'

const POLL_MS = 2000
const busy = (r: Repo) => r.status === 'queued' || r.status === 'fetching' || r.status === 'indexing'

function RepoStatus({ r }: { r: Repo }) {
  const m = r.meta
  if (r.status === 'queued') return <span className="kb-badge"><Loader2 size={11} className="spin" /> Queued</span>
  if (r.status === 'fetching') {
    return <span className="kb-badge"><Loader2 size={11} className="spin" /> {r.source === 'git' ? 'Cloning' : 'Reading archive'}</span>
  }
  if (r.status === 'indexing') {
    const pct = m.files ? Math.floor(((m.indexed ?? 0) / m.files) * 100) : 0
    return <span className="kb-badge"><Loader2 size={11} className="spin" /> Indexing {pct}%</span>
  }
  if (r.status === 'error') return <span className="kb-badge err" title={m.error ?? ''}><AlertTriangle size={11} /> Failed</span>
  return <span className="kb-badge ok">{m.files} files</span>
}

function describe(r: Repo): string {
  const m = r.meta
  if (r.status === 'error') return m.error ?? 'Indexing failed'
  const parts: string[] = []
  if (r.source === 'git') parts.push(`${r.ref ?? 'default branch'}${r.commit_sha ? ` @ ${r.commit_sha.slice(0, 7)}` : ''}`)
  if (m.langs?.length) parts.push(m.langs.slice(0, 3).join(', '))
  if (m.lines) parts.push(`${m.lines.toLocaleString()} lines`)
  if (r.status === 'indexing' && m.files) parts.push(`${m.indexed ?? 0}/${m.files} files`)
  if (m.skipped) parts.push(`${m.skipped} files over the size limit skipped`)
  if (r.status === 'ready' && m.synced_at) {
    parts.push(`synced ${relativeDay(m.synced_at).toLowerCase()}${m.sync_trigger === 'auto' ? ' (automatically)' : ''}`)
  }
  return parts.join(' · ')
}

/** Automatic sync state of a git repository, with an on/off switch for people who can edit the project. */
function AutoSync({ r, minutes, canEdit, onToggle }: { r: Repo; minutes: number; canEdit: boolean; onToggle: () => void }) {
  if (r.source !== 'git' || !minutes) return null
  const on = !!r.auto_sync
  const err = on ? r.meta.auto_sync_error : null
  const every = minutes % 60 === 0 ? `${minutes / 60} h` : `${minutes} min`
  return (
    <div className={`repo-auto${err ? ' warn' : ''}`}>
      {err ? <AlertTriangle size={11} /> : <RefreshCw size={11} />}
      <span title={err ?? (on ? `Checked for new commits every ${every}; re-indexed when the branch moves` : undefined)}>
        {!on ? 'Auto-sync off'
          : err ? `Auto-sync failing: ${err}`
          : `Auto-sync · ${r.checked_at ? `checked ${timeAgo(r.checked_at)}` : 'first check soon'}`}
      </span>
      {canEdit && <button className="link-btn" onClick={onToggle}>{on ? 'Turn off' : 'Turn on'}</button>}
    </div>
  )
}

/** Code repositories of a project: add from a .zip or a git URL, watch indexing, sync, remove. */
export function RepoSection({ pid, repos, canEdit, onChange }: {
  pid: string; repos: Repo[]; canEdit: boolean; onChange: () => Promise<void> | void
}) {
  const notify = useApp((s) => s.notify)
  const syncMinutes = useApp((s) => s.config?.features.repo_sync_minutes ?? 0)
  const [adding, setAdding] = useState(false)
  const [url, setUrl] = useState('')
  const [ref, setRef] = useState('')
  const [token, setToken] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [uploading, setUploading] = useState<string | null>(null)
  const zipInput = useRef<HTMLInputElement>(null)
  const replaceTarget = useRef<Repo | null>(null)

  const anyBusy = repos.some(busy)
  useEffect(() => {
    if (!anyBusy) return
    const t = setInterval(() => void onChange(), POLL_MS)
    return () => clearInterval(t)
  }, [anyBusy, onChange])

  const run = async (fn: () => Promise<unknown>) => {
    try { await fn() } catch (e) { notify((e as Error).message) }
    await onChange()
  }

  const addGit = async () => {
    if (!url.trim()) return
    setSubmitting(true)
    await run(async () => {
      await api.addGitRepo(pid, { url: url.trim(), ref: ref.trim() || undefined, token: token.trim() || undefined })
      setAdding(false); setUrl(''); setRef(''); setToken('')
    })
    setSubmitting(false)
  }

  const onZip = async (file: File | undefined) => {
    if (!file) return
    const target = replaceTarget.current
    replaceTarget.current = null
    setUploading(file.name)
    await run(() => (target ? api.replaceZipRepo(pid, target.id, file) : api.addZipRepo(pid, file)))
    setUploading(null)
  }

  return (
    <section className="ps-section">
      <div className="ps-row">
        <h4>Code</h4>
        {canEdit && !adding && (
          <button className="btn ghost sm" onClick={() => setAdding(true)}><Plus size={14} /> Add repository</button>
        )}
        <input ref={zipInput} type="file" accept=".zip,application/zip" hidden
          onChange={(e) => { void onZip(e.target.files?.[0]); e.target.value = '' }} />
      </div>
      {!repos.length && !adding && (
        <p className="muted small">Connect a git repository or upload a .zip, then ask questions about the actual code. Qwen can search, open and cite its files.</p>
      )}
      {adding && (
        <div className="repo-form">
          <input autoFocus placeholder="https://github.com/org/repo.git" value={url} onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && void addGit()} />
          <div className="field-row">
            <input placeholder="Branch or tag (optional)" value={ref} onChange={(e) => setRef(e.target.value)} />
            <input placeholder="Access token (private repos)" type="password" autoComplete="off" value={token}
              onChange={(e) => setToken(e.target.value)} />
          </div>
          <p className="muted small">Tokens are stored on this server only, to allow syncing. Use a read-only token.</p>
          <div className="row-end">
            <button className="btn ghost sm" onClick={() => { replaceTarget.current = null; zipInput.current?.click() }}>
              <Upload size={14} /> Upload .zip instead
            </button>
            <span className="grow" />
            <button className="btn ghost sm" onClick={() => setAdding(false)}>Cancel</button>
            <button className="btn primary sm" disabled={!url.trim() || submitting} onClick={() => void addGit()}>
              {submitting ? <Loader2 size={14} className="spin" /> : <GitBranch size={14} />} Connect
            </button>
          </div>
        </div>
      )}
      <div className="ps-files">
        {repos.map((r) => (
          <div key={r.id} className="repo-row">
            <div className="ps-file">
              {r.source === 'git' ? <GitBranch size={16} /> : <FileArchive size={16} />}
              <span className="ps-file-name" title={r.url ?? `${r.name} · uploaded by ${r.added_by ?? 'someone'}`}>{r.name}</span>
              <RepoStatus r={r} />
              {r.meta.bytes ? <span className="muted small">{formatBytes(r.meta.bytes)}</span> : null}
              {canEdit && (
                <>
                  {r.source === 'git' ? (
                    <button className="icon-btn sm" title="Sync latest commit" disabled={busy(r)}
                      onClick={() => void run(() => api.syncRepo(pid, r.id))}><RefreshCw size={13} /></button>
                  ) : r.status === 'error' && r.meta.files ? (
                    <button className="icon-btn sm" title="Retry indexing" onClick={() => void run(() => api.syncRepo(pid, r.id))}><RefreshCw size={13} /></button>
                  ) : (
                    <button className="icon-btn sm" title="Upload a new version (.zip)" disabled={busy(r)}
                      onClick={() => { replaceTarget.current = r; zipInput.current?.click() }}><Upload size={13} /></button>
                  )}
                  <button className="icon-btn sm" title="Remove repository" onClick={() => {
                    if (confirm(`Remove “${r.name}” from this project? Its index is deleted; chats keep their answers.`)) {
                      void run(() => api.deleteRepo(pid, r.id))
                    }
                  }}><Trash2 size={13} /></button>
                </>
              )}
            </div>
            <div className={`repo-desc${r.status === 'error' ? ' err' : ''}`}>{describe(r)}</div>
            <AutoSync r={r} minutes={syncMinutes} canEdit={canEdit}
              onToggle={() => void run(() => api.setRepoAutoSync(pid, r.id, !r.auto_sync))} />
            {r.status === 'indexing' && !!r.meta.files && (
              <div className="repo-progress"><span style={{ width: `${Math.max(3, ((r.meta.indexed ?? 0) / r.meta.files) * 100)}%` }} /></div>
            )}
          </div>
        ))}
        {uploading && <div className="ps-file"><Loader2 size={16} className="spin" /> Uploading {uploading}…</div>}
      </div>
    </section>
  )
}
