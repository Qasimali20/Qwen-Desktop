import { useEffect, useState } from 'react'
import { Loader2, Lock, Pencil, Plus, Trash2, User as UserIcon, Users, Zap } from 'lucide-react'
import { api } from '../api/client'
import type { PromptShortcut } from '../api/types'
import { useApp } from '../store/app'

type Draft = Pick<PromptShortcut, 'command' | 'title' | 'description' | 'body'> & { scope: PromptShortcut['scope'] }

const EMPTY: Draft = { command: '', title: '', description: '', body: '', scope: 'personal' }

function ShortcutForm({ initial, existing, onDone }: { initial?: PromptShortcut; existing: boolean; onDone: () => void }) {
  const { user, notify, refreshPrompts } = useApp()
  const [d, setD] = useState<Draft>(initial ? { ...initial } : EMPTY)
  const [busy, setBusy] = useState(false)
  const set = (k: keyof Draft) => (e: { target: { value: string } }) => setD({ ...d, [k]: e.target.value })

  const save = async () => {
    setBusy(true)
    try {
      const { scope, ...fields } = d
      if (initial) await api.updatePrompt(initial.id, fields)
      else await api.createPrompt({ ...fields, scope })
      await refreshPrompts()
      notify(initial ? `Saved /${d.command}` : `Created /${d.command}`)
      onDone()
    } catch (e) { notify((e as Error).message) } finally { setBusy(false) }
  }

  return (
    <form className="card form shortcut-form" onSubmit={(e) => { e.preventDefault(); void save() }}>
      <div className="form-grid">
        <label className="field-inline">
          <span className="slash-prefix">/</span>
          <input placeholder="command" value={d.command} required pattern="[a-z0-9][a-z0-9\-]{0,31}" autoFocus={!initial}
            title="Lowercase letters, digits and dashes"
            onChange={(e) => setD({ ...d, command: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, '') })} />
        </label>
        <input placeholder="Title (e.g. Code review)" value={d.title} onChange={set('title')} required maxLength={80} />
        <input className="span-2" placeholder="Short description shown in the picker (optional)" value={d.description}
          onChange={set('description')} maxLength={200} />
        <textarea className="span-2 mono" rows={8} required value={d.body} onChange={set('body')}
          placeholder={'The instructions sent to Qwen.\n\nUse {{input}} where the user\'s text should go — otherwise it is added at the end.'} />
        {!initial && user?.role === 'admin' && (
          <select value={d.scope} onChange={set('scope')}>
            <option value="personal">Personal — only you</option>
            <option value="team">Team — everyone</option>
          </select>
        )}
      </div>
      {existing && !initial && <p className="muted small">A personal shortcut with the same command as a team one replaces it for you.</p>}
      <div className="row-end">
        <button type="button" className="btn ghost" onClick={onDone}>Cancel</button>
        <button className="btn primary" disabled={busy}>{busy ? <Loader2 size={14} className="spin" /> : initial ? 'Save' : 'Create shortcut'}</button>
      </div>
    </form>
  )
}

function ShortcutRow({ p, shadowed, onEdit }: { p: PromptShortcut; shadowed: boolean; onEdit: () => void }) {
  const { notify, refreshPrompts } = useApp()
  return (
    <div className={`card shortcut-row${shadowed ? ' dim' : ''}`}>
      <div className="shortcut-head">
        <code className="slash-cmd">/{p.command}</code>
        <strong>{p.title}</strong>
        {shadowed && <span className="badge">Replaced by your personal /{p.command}</span>}
        <span className="grow" />
        {p.editable ? (
          <>
            <button className="icon-btn sm" title="Edit" onClick={onEdit}><Pencil size={14} /></button>
            <button className="icon-btn sm danger" title="Delete" onClick={async () => {
              if (!confirm(`Delete /${p.command}${p.scope === 'team' ? ' for the whole team' : ''}?`)) return
              try { await api.deletePrompt(p.id); await refreshPrompts() } catch (e) { notify((e as Error).message) }
            }}><Trash2 size={14} /></button>
          </>
        ) : <span className="muted small" title="Only admins can change team shortcuts"><Lock size={12} /> Team</span>}
      </div>
      {p.description && <p className="muted small">{p.description}</p>}
      <pre className="shortcut-body">{p.body}</pre>
    </div>
  )
}

export function ShortcutsView() {
  const { prompts, user, refreshPrompts } = useApp()
  const [editing, setEditing] = useState<string | 'new' | null>(null)
  useEffect(() => { void refreshPrompts() }, [refreshPrompts])

  const personal = prompts.filter((p) => p.scope === 'personal')
  const team = prompts.filter((p) => p.scope === 'team')
  const mine = new Set(personal.map((p) => p.command))

  const list = (items: PromptShortcut[]) => items.map((p) => editing === p.id
    ? <ShortcutForm key={p.id} initial={p} existing onDone={() => setEditing(null)} />
    : <ShortcutRow key={p.id} p={p} shadowed={p.scope === 'team' && mine.has(p.command)} onEdit={() => setEditing(p.id)} />)

  return (
    <div className="page">
      <div className="page-inner">
        <div className="page-head">
          <h1><Zap size={22} className="accent-icon" /> Shortcuts</h1>
          {editing !== 'new' && <button className="btn primary" onClick={() => setEditing('new')}><Plus size={16} /> New shortcut</button>}
        </div>
        <p className="muted">
          Type <kbd>/</kbd> in any chat to use a shortcut: pick one, paste your code, error or notes, and send.
          Qwen receives the shortcut's instructions with your text filled in.
        </p>
        {editing === 'new' && <ShortcutForm existing={false} onDone={() => setEditing(null)} />}

        <h3 className="section-title"><UserIcon size={15} /> Your shortcuts</h3>
        {personal.length ? list(personal) : <p className="muted small">None yet. Create your own, or copy a team one and tweak it.</p>}

        <h3 className="section-title"><Users size={15} /> Team shortcuts
          {user?.role !== 'admin' && <span className="muted small"> · managed by admins</span>}</h3>
        {list(team)}
      </div>
    </div>
  )
}
