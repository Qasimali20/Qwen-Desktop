import { useCallback, useEffect, useRef, useState } from 'react'
import { FileText, FolderOpen, Image as ImageIcon, Lock, Loader2, Plus, Trash2, Upload, Users } from 'lucide-react'
import { api } from '../api/client'
import type { Project, ProjectDetail, TeamAccess } from '../api/types'
import { formatBytes, relativeDay } from '../lib/text'
import { useApp } from '../store/app'
import { ChatView } from './ChatView'
import { RepoSection } from './RepoSection'
import { WorkspaceSection } from './WorkspaceSection'

const ACCESS: Record<TeamAccess, { label: string; hint: string }> = {
  none: { label: 'Private', hint: 'Only you can see and use this project' },
  view: { label: 'Team can use', hint: 'Everyone can chat with its instructions and files; only you can change them' },
  edit: { label: 'Team can edit', hint: 'Everyone can chat with it and change its instructions and files' },
}

function SharingBadge({ p }: { p: Project }) {
  if (p.team_access === 'none') return null
  return (
    <span className="badge accent" title={ACCESS[p.team_access].hint}>
      <Users size={11} /> {p.permissions?.owner ? ACCESS[p.team_access].label : `Shared by ${p.owner_name ?? 'a teammate'}`}
    </span>
  )
}

export function ProjectsView() {
  const { projects, navigate, refreshProjects, user } = useApp()
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [access, setAccess] = useState<TeamAccess>('none')

  useEffect(() => { void refreshProjects() }, [refreshProjects])

  const mine = projects.filter((p) => p.user_id === user?.id)
  const shared = projects.filter((p) => p.user_id !== user?.id)

  const create = async () => {
    if (!name.trim()) return
    const p = await api.createProject({ name: name.trim(), description, team_access: access })
    await refreshProjects()
    navigate({ name: 'project', id: p.id })
  }

  return (
    <div className="page">
      <div className="page-inner">
        <div className="page-head">
          <h1>Projects</h1>
          <button className="btn primary" onClick={() => setCreating(true)}><Plus size={16} /> New project</button>
        </div>
        <p className="muted">Projects bundle instructions and reference files that every chat inside them can use.</p>
        {creating && (
          <div className="card form">
            <input autoFocus placeholder="Project name" value={name} onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && void create()} />
            <input placeholder="What is it about? (optional)" value={description} onChange={(e) => setDescription(e.target.value)} />
            <select value={access} onChange={(e) => setAccess(e.target.value as TeamAccess)} title={ACCESS[access].hint}>
              {(Object.keys(ACCESS) as TeamAccess[]).map((k) => <option key={k} value={k}>{ACCESS[k].label} · {ACCESS[k].hint}</option>)}
            </select>
            <div className="row-end">
              <button className="btn ghost" onClick={() => setCreating(false)}>Cancel</button>
              <button className="btn primary" disabled={!name.trim()} onClick={() => void create()}>Create</button>
            </div>
          </div>
        )}
        {[['Your projects', mine], ['Shared with the team', shared]].map(([title, list]) => (list as Project[]).length > 0 && (
          <section key={title as string}>
            {shared.length > 0 && <h3 className="section-title">{title as string}</h3>}
            <div className="project-grid">
              {(list as Project[]).map((p) => (
                <button key={p.id} className="card project-card" onClick={() => navigate({ name: 'project', id: p.id })}>
                  <span className="project-card-top"><FolderOpen size={18} /><SharingBadge p={p} /></span>
                  <h3>{p.name}</h3>
                  <p>{p.description || 'No description'}</p>
                  <span className="muted small">{p.chat_count ?? 0} chats · {p.file_count ?? 0} files · updated {relativeDay(p.updated_at).toLowerCase()}</span>
                </button>
              ))}
            </div>
          </section>
        ))}
        {!projects.length && !creating && <p className="muted">No projects yet.</p>}
      </div>
    </div>
  )
}

export function ProjectView({ id }: { id: string }) {
  const { navigate, notify, refreshProjects } = useApp()
  const [p, setP] = useState<ProjectDetail | null>(null)
  const [instructions, setInstructions] = useState('')
  const [uploading, setUploading] = useState(0)
  const fileInput = useRef<HTMLInputElement>(null)

  const reload = useCallback(async () => {
    const d = await api.project(id)
    setP(d)
    setInstructions(d.instructions)
  }, [id])

  useEffect(() => { void reload().catch(() => navigate({ name: 'projects' }, true)) }, [reload, navigate])

  // Poll while documents are being indexed.
  const indexing = !!p?.files.some((f) => f.meta.kb?.status === 'indexing')
  useEffect(() => {
    if (!indexing) return
    const t = setInterval(() => void reload(), 2500)
    return () => clearInterval(t)
  }, [indexing, reload])

  if (!p) return <div className="page"><div className="page-inner"><Loader2 className="spin" /></div></div>
  const can = p.permissions ?? { owner: true, manage: true, edit: true }

  const upload = async (files: FileList | null) => {
    if (!files?.length) return
    setUploading((n) => n + files.length)
    await Promise.all(Array.from(files).map((f) =>
      api.uploadProjectFile(id, f).catch((e) => notify(`${f.name}: ${(e as Error).message}`)).finally(() => setUploading((n) => n - 1))))
    await reload()
    void refreshProjects()
  }

  const totalChars = p.files.reduce((s, f) => s + (f.meta.chars ?? 0), 0)
  // Mirrors the server rule: docs are inlined while they fit in ~35% of the largest model context.
  const maxContext = Math.max(...(useApp.getState().config?.models.map((m) => m.context) ?? [16384]))
  const retrieval = totalChars > maxContext * 3 * 0.35
  const kbLabel = (f: (typeof p.files)[number]) => {
    const kb = f.meta.kb
    if (f.kind !== 'document' || !kb) return null
    if (kb.status === 'indexing') return <span className="kb-badge"><Loader2 size={11} className="spin" /> Indexing</span>
    if (kb.status === 'error') return <span className="kb-badge err" title={kb.error}>Failed</span>
    return <span className="kb-badge ok">{kb.chunks} chunks</span>
  }

  return (
    <div className="project-layout">
      <div className="project-main">
        <ChatView projectId={id} />
        {!!p.conversations.length && (
          <div className="project-chats">
            <h4>Chats in this project</h4>
            {p.conversations.map((c) => (
              <button key={c.id} className="project-chat" onClick={() => navigate({ name: 'chat', id: c.id })}>
                <span>{c.title}</span><span className="muted small">{relativeDay(c.updated_at)}</span>
              </button>
            ))}
          </div>
        )}
      </div>
      <aside className="project-side">
        <div className="ps-head">
          <h2>{p.name}</h2>
          {can.manage && <button className="icon-btn sm" title="Delete project" onClick={async () => {
            const msg = p.team_access === 'none' ? `Delete project “${p.name}” and all its chats?`
              : `Delete project “${p.name}” for everyone? Your chats in it are deleted; teammates keep theirs, without the project.`
            if (!confirm(msg)) return
            await api.deleteProject(id)
            await Promise.all([refreshProjects(), useApp.getState().refreshConversations()])
            navigate({ name: 'projects' }, true)
          }}><Trash2 size={14} /></button>}
        </div>
        {p.description && <p className="muted">{p.description}</p>}

        <section className="ps-section">
          <h4>Sharing</h4>
          {can.manage ? (
            <>
              <select value={p.team_access} onChange={async (e) => {
                const team_access = e.target.value as TeamAccess
                try {
                  setP({ ...p, ...(await api.patchProject(id, { team_access })) })
                  void refreshProjects()
                } catch (err) { notify((err as Error).message) }
              }}>
                {(Object.keys(ACCESS) as TeamAccess[]).map((k) => <option key={k} value={k}>{ACCESS[k].label}</option>)}
              </select>
              <p className="muted small">{ACCESS[p.team_access].hint}. Chats always stay private to each person.</p>
            </>
          ) : (
            <p className="muted small">
              <Users size={12} /> Shared by {p.owner_name}. {can.edit ? 'You can change its instructions and files.' : <><Lock size={11} /> Read-only for you.</>} Your chats here are private to you.
            </p>
          )}
        </section>

        <section className="ps-section">
          <h4>Instructions</h4>
          <textarea placeholder={can.edit ? 'How should Qwen behave in this project? Tone, role, format, constraints…' : 'No instructions'}
            value={instructions} rows={6} readOnly={!can.edit} onChange={(e) => setInstructions(e.target.value)}
            onBlur={() => can.edit && instructions !== p.instructions && void api.patchProject(id, { instructions })
              .then(() => setP({ ...p, instructions })).catch((e) => notify((e as Error).message))} />
        </section>

        <RepoSection pid={id} repos={p.repos ?? []} canEdit={can.edit} onChange={reload} />
        <WorkspaceSection pid={id} repos={p.repos ?? []} />

        <section className="ps-section">
          <div className="ps-row">
            <h4>Knowledge</h4>
            {can.edit && <button className="btn ghost sm" onClick={() => fileInput.current?.click()}><Upload size={14} /> Add files</button>}
            <input ref={fileInput} type="file" multiple hidden onChange={(e) => { void upload(e.target.files); e.target.value = '' }} />
          </div>
          <p className="muted small">
            {!totalChars ? 'Add documents for Qwen to use in every chat in this project.'
              : retrieval
                ? `~${Math.round(totalChars / 3000)}k tokens — too large to include in full, so the most relevant passages are retrieved for each question and cited.`
                : `~${totalChars < 3000 ? '<1' : Math.round(totalChars / 3000)}k tokens — included in full in every chat.`}
          </p>
          {!!totalChars && <span className={`kb-mode${retrieval ? ' on' : ''}`}>{retrieval ? 'Retrieval mode' : 'Full-context mode'}</span>}
          <div className="ps-files">
            {p.files.map((f) => (
              <div key={f.id} className="ps-file">
                {f.kind === 'image' ? <ImageIcon size={16} /> : <FileText size={16} />}
                <span className="ps-file-name" title={f.meta.uploaded_by ? `${f.filename} · added by ${f.meta.uploaded_by}` : f.filename}>{f.filename}</span>
                {kbLabel(f)}
                <span className="muted small">{f.meta.pages ? `${f.meta.pages}p` : formatBytes(f.size)}</span>
                {can.edit && (
                  <button className="icon-btn sm" onClick={async () => { await api.deleteProjectFile(id, f.id); void reload() }}>
                    <Trash2 size={13} />
                  </button>
                )}
              </div>
            ))}
            {uploading > 0 && <div className="ps-file"><Loader2 size={16} className="spin" /> Processing {uploading} file(s)…</div>}
          </div>
        </section>
      </aside>
    </div>
  )
}
