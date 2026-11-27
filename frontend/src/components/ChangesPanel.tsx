import { createPortal } from 'react-dom'
import { useCallback, useEffect, useState } from 'react'
import { Check, Download, ExternalLink, FileArchive, FolderGit2, GitCommitHorizontal, Loader2, RotateCcw, Upload, X } from 'lucide-react'
import { api } from '../api/client'
import type { PushInfo, Workspace, WorkspaceChange, WorkspaceChanges } from '../api/types'
import { useApp } from '../store/app'
import { useChat } from '../store/chat'
import { DiffView } from './DiffView'

const STATUS: Record<string, { label: string; cls: string }> = {
  A: { label: 'Added', cls: 'add' }, M: { label: 'Modified', cls: 'mod' }, D: { label: 'Deleted', cls: 'del' },
  R: { label: 'Renamed', cls: 'mod' }, T: { label: 'Type changed', cls: 'mod' },
}

function defaultBranch(ws: Workspace): string {
  const d = new Date()
  const stamp = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`
  return `qwen/${ws.name.toLowerCase().replace(/[^a-z0-9._-]+/g, '-')}-${stamp}`
}

/** Header chip of a chat working in a workspace: shows how many files changed; opens the review panel. */
export function WorkspaceChip({ workspaceId, name }: { workspaceId: string; name: string }) {
  const streaming = useChat((s) => !!s.streaming)
  const [count, setCount] = useState<number | null>(null)
  const [open, setOpen] = useState(false)

  const refresh = useCallback(() => {
    api.workspaceChanges(workspaceId).then((c) => setCount(c.files.length)).catch(() => setCount(null))
  }, [workspaceId])
  // Refresh when a reply finishes (the model may have changed files) and when the panel closes.
  useEffect(() => { if (!streaming) refresh() }, [streaming, refresh])

  return (
    <>
      <button className={`btn ghost sm ws-chip${count ? ' dirty' : ''}`} onClick={() => setOpen(true)}
        title="Review the files Qwen changed in this workspace">
        <FolderGit2 size={14} /> <span className="ws-chip-name">{name}</span>
        {count ? <span className="ws-count">{count}</span> : null}
      </button>
      {open && <ChangesPanel workspaceId={workspaceId} onClose={() => { setOpen(false); refresh() }} />}
    </>
  )
}

export function ChangesPanel({ workspaceId, onClose }: { workspaceId: string; onClose: () => void }) {
  const notify = useApp((s) => s.notify)
  const [ws, setWs] = useState<Workspace | null>(null)
  const [changes, setChanges] = useState<WorkspaceChanges | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [diff, setDiff] = useState<{ path: string; text: string; truncated: boolean } | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [committing, setCommitting] = useState(false)
  const [message, setMessage] = useState('')
  const [branchInput, setBranch] = useState<string | null>(null)
  const branch = branchInput ?? (ws ? defaultBranch(ws) : '')
  const [push, setPush] = useState(true)
  const [pushed, setPushed] = useState<PushInfo | null>(null)

  const apply = useCallback((w: Workspace, c: WorkspaceChanges) => {
    setWs(w)
    setChanges(c)
    setSelected((cur) => (cur && c.files.some((f) => f.path === cur) ? cur : c.files[0]?.path ?? null))
  }, [])
  const fetchBoth = useCallback(() => Promise.all([api.workspace(workspaceId), api.workspaceChanges(workspaceId)]), [workspaceId])
  const load = useCallback(async () => { apply(...(await fetchBoth())) }, [apply, fetchBoth])

  useEffect(() => {
    let live = true
    fetchBoth().then((r) => live && apply(...r)).catch((e) => { notify((e as Error).message); onClose() })
    return () => { live = false }
  }, [fetchBoth, apply, notify, onClose])

  useEffect(() => {
    if (!selected) return
    let live = true
    api.workspaceDiff(workspaceId, selected)
      .then((d) => live && setDiff({ path: selected, text: d.diff, truncated: d.truncated }))
      .catch((e) => live && notify((e as Error).message))
    return () => { live = false }
  }, [selected, workspaceId, changes, notify])

  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])

  const act = async (label: string, fn: () => Promise<void>) => {
    setBusy(label)
    try { await fn() } catch (e) { notify((e as Error).message) } finally { setBusy(null) }
  }

  const discard = (f?: WorkspaceChange) => act('discard', async () => {
    const what = f ? `changes to ${f.path}` : 'ALL uncommitted changes in this workspace'
    if (!confirm(`Discard ${what}? This cannot be undone.`)) return
    setChanges(await api.discardWorkspace(workspaceId, f?.path))
    await load()
  })

  const commit = () => act('commit', async () => {
    const res = await api.commitWorkspace(workspaceId, { message: message.trim(), push: push && !!ws?.can_push, branch: branch.trim() })
    setWs(res.workspace)
    setPushed(res.pushed)
    setCommitting(false)
    setMessage('')
    await load()
    notify(res.pushed ? `Committed and pushed to ${res.pushed.branch}` : 'Changes committed in the workspace')
  })

  const retryPush = () => act('push', async () => {
    const res = await api.pushWorkspace(workspaceId, branch.trim())
    setWs(res.workspace)
    setPushed(res.pushed)
  })

  const files = changes?.files ?? []
  const unpushed = !!ws && !!changes && changes.head !== ws.base_sha && ws.meta.pushed?.sha !== changes.head
  const lastPush = pushed ?? ws?.meta.pushed ?? null

  // Portalled: an ancestor in the chat header is transformed, which would trap a fixed overlay inside it.
  return createPortal(
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal changes-panel" role="dialog" aria-label="Workspace changes">
        <header>
          <div className="cv-title">
            <FolderGit2 size={16} />
            <span className="cv-path">{ws?.name ?? 'Workspace'}</span>
            {ws?.repo && <span className="muted small">{ws.repo.name}{ws.base_ref ? ` @ ${ws.base_ref}` : ''}</span>}
          </div>
          <button className="icon-btn" aria-label="Close" onClick={onClose}><X size={18} /></button>
        </header>

        {!changes ? (
          <div className="changes-loading muted"><Loader2 size={16} className="spin" /> Loading changes…</div>
        ) : (
          <div className="changes-body">
            <aside className="changes-files">
              <div className="changes-files-head muted small">
                {files.length ? `${files.length} changed file${files.length === 1 ? '' : 's'}${changes.truncated ? '+' : ''}` : 'No uncommitted changes'}
              </div>
              {files.map((f) => {
                const st = STATUS[f.status] ?? { label: f.status, cls: 'mod' }
                return (
                  <button key={f.path} className={`changes-file${selected === f.path ? ' on' : ''}`} onClick={() => setSelected(f.path)}
                    title={f.old_path ? `${f.old_path} → ${f.path}` : f.path}>
                    <span className={`changes-status ${st.cls}`} aria-label={st.label}>{f.status}</span>
                    <span className="changes-path">{f.path}</span>
                    <span className="changes-stat">
                      {f.added !== null && <span className="add">+{f.added}</span>}
                      {f.removed !== null && <span className="del">−{f.removed}</span>}
                    </span>
                  </button>
                )
              })}
            </aside>
            <section className="changes-diff">
              {selected && diff?.path === selected ? (
                <>
                  <div className="changes-diff-head">
                    <span className="cv-path">{selected}</span>
                    <button className="btn ghost sm" disabled={!!busy} onClick={() => void discard(files.find((f) => f.path === selected))}>
                      <RotateCcw size={13} /> Discard file
                    </button>
                  </div>
                  <DiffView diff={diff.text} empty="No textual changes (binary file or mode change)" />
                  {diff.truncated && <p className="muted small">Diff truncated; download the patch for the full change.</p>}
                </>
              ) : selected ? (
                <div className="changes-loading muted"><Loader2 size={16} className="spin" /></div>
              ) : (
                <div className="changes-empty muted">
                  <Check size={22} />
                  <p>Nothing to review. Ask Qwen to make a change in this workspace, then come back here.</p>
                  {unpushed && ws?.can_push && <p>Committed work is ready to push.</p>}
                </div>
              )}
            </section>
          </div>
        )}

        {lastPush && (
          <div className="changes-pushed small">
            <Check size={14} /> Pushed to <strong>{lastPush.branch}</strong> ({lastPush.sha.slice(0, 7)})
            {lastPush.url && <a href={lastPush.url} target="_blank" rel="noreferrer">Open pull request <ExternalLink size={12} /></a>}
          </div>
        )}

        {committing ? (
          <div className="changes-commit">
            <input autoFocus placeholder="Commit message (describe what changed)" value={message} maxLength={500}
              onChange={(e) => setMessage(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && message.trim() && void commit()} />
            {ws?.can_push && (
              <div className="changes-push-row">
                <label className="switch-inline"><input type="checkbox" checked={push} onChange={(e) => setPush(e.target.checked)} /> Push to branch</label>
                <input className="mono" value={branch} disabled={!push} onChange={(e) => setBranch(e.target.value)} aria-label="Branch name" />
              </div>
            )}
            {ws?.can_push && push && <p className="muted small">Pushing needs the repository's access token to have write access. Existing branches are never overwritten.</p>}
            <div className="changes-actions">
              <button className="btn ghost sm" onClick={() => setCommitting(false)}>Cancel</button>
              <button className="btn primary sm" disabled={!message.trim() || !!busy} onClick={() => void commit()}>
                {busy === 'commit' ? <Loader2 size={14} className="spin" /> : push && ws?.can_push ? <Upload size={14} /> : <GitCommitHorizontal size={14} />}
                {push && ws?.can_push ? 'Commit & push' : 'Commit'}
              </button>
            </div>
          </div>
        ) : (
          <footer className="changes-actions">
            <a className="btn ghost sm" href={api.workspacePatchUrl(workspaceId)} aria-disabled={!files.length}><Download size={14} /> Patch</a>
            <a className="btn ghost sm" href={api.workspaceArchiveUrl(workspaceId)}><FileArchive size={14} /> Download .zip</a>
            <span className="grow" />
            {!!files.length && <button className="btn ghost sm danger-text" disabled={!!busy} onClick={() => void discard()}><RotateCcw size={14} /> Discard all</button>}
            {unpushed && ws?.can_push && !files.length && (
              <button className="btn ghost sm" disabled={!!busy} onClick={() => void retryPush()}>
                {busy === 'push' ? <Loader2 size={14} className="spin" /> : <Upload size={14} />} Push to {branch}
              </button>
            )}
            <button className="btn primary sm" disabled={!files.length || !!busy} onClick={() => setCommitting(true)}>
              <GitCommitHorizontal size={14} /> Commit{ws?.can_push ? ' & push' : ''}…
            </button>
          </footer>
        )}
      </div>
    </div>,
    document.body,
  )
}
