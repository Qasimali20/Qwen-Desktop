import { useEffect, useState } from 'react'
import { AlertTriangle, Download, FolderGit2, FolderPlus, Loader2, MessageSquarePlus, Plus, Trash2 } from 'lucide-react'
import { api } from '../api/client'
import type { Repo, Workspace } from '../api/types'
import { relativeDay } from '../lib/text'
import { useApp } from '../store/app'
import { useWorkspaces } from '../store/workspaces'

const POLL_MS = 2000
const EMPTY = '__empty__'

function wsDescription(w: Workspace): string {
  if (w.status === 'error') return w.meta.error ?? 'Could not be created'
  if (w.status === 'creating') return w.repo?.source === 'git' ? 'Cloning…' : 'Preparing…'
  const parts = [w.repo ? `${w.repo.name}${w.base_ref ? ` @ ${w.base_ref}` : ''}` : 'Empty']
  if (w.chats) parts.push(`${w.chats} chat${w.chats === 1 ? '' : 's'}`)
  if (w.meta.pushed) parts.push(`pushed to ${w.meta.pushed.branch}`)
  parts.push(`active ${relativeDay(w.updated_at).toLowerCase()}`)
  return parts.join(' · ')
}

/** Focus the project's composer so the user can type the task straight away. */
function focusComposer() {
  requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('.project-main .composer textarea')?.focus())
}

/** Project side panel: the user's workspaces — writable copies of the code that Qwen edits and runs. */
export function WorkspaceSection({ pid, repos }: { pid: string; repos: Repo[] }) {
  const notify = useApp((s) => s.notify)
  const chosen = useApp((s) => s.workspaceChoice[pid] ?? null)
  const chooseWorkspace = useApp((s) => s.chooseWorkspace)
  const { byProject, load, put, remove } = useWorkspaces()
  const list = byProject[pid]
  const [adding, setAdding] = useState(false)
  const [source, setSource] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)

  const usable = repos.filter((r) => (r.source === 'git' ? !!r.commit_sha : !!r.meta.files))
  const source_ = source || usable[0]?.id || EMPTY

  useEffect(() => { void load(pid).catch(() => {}) }, [pid, load])

  const creating = !!list?.some((w) => w.status === 'creating')
  useEffect(() => {
    if (!creating) return
    const t = setInterval(() => void load(pid).catch(() => {}), POLL_MS)
    return () => clearInterval(t)
  }, [creating, pid, load])

  const create = async () => {
    setBusy(true)
    try {
      const ws = await api.createWorkspace(pid, { name: name.trim() || undefined, repo_id: source_ === EMPTY ? null : source_ })
      put(ws)
      chooseWorkspace(pid, ws.id)
      setAdding(false)
      setName('')
    } catch (e) {
      notify((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const del = async (w: Workspace) => {
    if (!confirm(`Delete workspace “${w.name}”? Its files and any changes that were not downloaded or pushed are lost. Chats keep their history.`)) return
    try {
      await api.deleteWorkspace(w.id)
      remove(pid, w.id)
      if (chosen === w.id) chooseWorkspace(pid, null)
    } catch (e) {
      notify((e as Error).message)
    }
  }

  return (
    <section className="ps-section">
      <div className="ps-row">
        <h4>Workspaces</h4>
        {!adding && <button className="btn ghost sm" onClick={() => setAdding(true)}><Plus size={14} /> New workspace</button>}
      </div>
      {!list?.length && !adding && (
        <p className="muted small">
          A workspace is your own working copy of the code on the server. Qwen creates and edits files there and runs
          commands and tests; you review the changes, then download them or push them to a branch.
        </p>
      )}
      {adding && (
        <div className="repo-form">
          <label className="ws-field">
            <span>Start from</span>
            <select value={source_} onChange={(e) => setSource(e.target.value)}>
              {usable.map((r) => <option key={r.id} value={r.id}>{r.name} ({r.source === 'git' ? `git${r.ref ? ` @ ${r.ref}` : ''}` : 'uploaded .zip'})</option>)}
              <option value={EMPTY}>Empty folder</option>
            </select>
          </label>
          <input placeholder="Name (optional)" value={name} maxLength={60} onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && void create()} />
          {source_ !== EMPTY && usable.find((r) => r.id === source_)?.source === 'zip' && (
            <p className="muted small">Uploaded archives are recreated from their indexed text files (binary files are not included).</p>
          )}
          <div className="row-end">
            <span className="grow" />
            <button className="btn ghost sm" onClick={() => setAdding(false)}>Cancel</button>
            <button className="btn primary sm" disabled={busy} onClick={() => void create()}>
              {busy ? <Loader2 size={14} className="spin" /> : <FolderPlus size={14} />} Create
            </button>
          </div>
        </div>
      )}
      <div className="ps-files">
        {list?.map((w) => (
          <div key={w.id} className={`repo-row ws-row${chosen === w.id ? ' chosen' : ''}`}>
            <div className="ps-file">
              {w.status === 'creating' ? <Loader2 size={16} className="spin" /> : w.status === 'error' ? <AlertTriangle size={16} /> : <FolderGit2 size={16} />}
              <span className="ps-file-name" title={w.name}>{w.name}</span>
              {w.status === 'ready' && (
                <>
                  <button className="icon-btn sm" title="New chat in this workspace"
                    onClick={() => { chooseWorkspace(pid, w.id); focusComposer() }}><MessageSquarePlus size={13} /></button>
                  <a className="icon-btn sm" title="Download as .zip" href={api.workspaceArchiveUrl(w.id)}><Download size={13} /></a>
                </>
              )}
              <button className="icon-btn sm" title="Delete workspace" onClick={() => void del(w)}><Trash2 size={13} /></button>
            </div>
            <div className={`repo-desc${w.status === 'error' ? ' err' : ''}`}>{wsDescription(w)}</div>
          </div>
        ))}
      </div>
    </section>
  )
}

/** Above the project composer: which workspace a new chat works in. */
export function WorkspacePicker({ projectId }: { projectId: string }) {
  const list = useWorkspaces((s) => s.byProject[projectId])
  const load = useWorkspaces((s) => s.load)
  const chosen = useApp((s) => s.workspaceChoice[projectId] ?? null)
  const chooseWorkspace = useApp((s) => s.chooseWorkspace)
  useEffect(() => { if (!list) void load(projectId).catch(() => {}) }, [list, load, projectId])
  const ready = (list ?? []).filter((w) => w.status === 'ready')
  if (!ready.length) return null
  const valid = ready.some((w) => w.id === chosen) ? chosen : null
  return (
    <label className={`ws-picker${valid ? ' on' : ''}`} title="Qwen can create and edit files and run commands in the chosen workspace">
      <FolderGit2 size={14} />
      <select value={valid ?? ''} onChange={(e) => chooseWorkspace(projectId, e.target.value || null)} aria-label="Workspace">
        <option value="">No workspace (chat only)</option>
        {ready.map((w) => <option key={w.id} value={w.id}>Workspace: {w.name}</option>)}
      </select>
    </label>
  )
}
