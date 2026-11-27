import { useEffect, useMemo, useState } from 'react'
import { ChevronDown, Copy, GraduationCap, Loader2, Lock, Pencil, Plus, Search, Sparkles, Trash2, User as UserIcon, Users } from 'lucide-react'
import { api } from '../api/client'
import type { Skill } from '../api/types'
import { useApp } from '../store/app'
import { Markdown } from './Markdown'

type Draft = Pick<Skill, 'name' | 'title' | 'description' | 'body'> & { scope: 'personal' | 'team' }

const EMPTY: Draft = { name: '', title: '', description: '', body: '', scope: 'personal' }
const MAX_BODY = 12_000

const SCOPE_LABEL: Record<Skill['scope'], string> = { builtin: 'Built-in', team: 'Team', personal: 'Yours' }

function SkillForm({ initial, copyOf, onDone }: { initial?: Skill; copyOf?: Skill; onDone: () => void }) {
  const { user, notify, refreshSkills } = useApp()
  const [d, setD] = useState<Draft>(() => initial ? { ...initial, scope: initial.scope === 'team' ? 'team' : 'personal' }
    : copyOf ? { name: `${copyOf.name}-custom`.slice(0, 40), title: `${copyOf.title} (custom)`.slice(0, 80),
      description: copyOf.description, body: copyOf.body, scope: 'personal' } : EMPTY)
  const [busy, setBusy] = useState(false)
  const set = (k: keyof Draft) => (e: { target: { value: string } }) => setD({ ...d, [k]: e.target.value })

  const save = async () => {
    setBusy(true)
    try {
      const { scope, ...fields } = d
      if (initial) await api.updateSkill(initial.id, fields)
      else await api.createSkill({ ...fields, scope })
      await refreshSkills()
      notify(initial ? `Saved ${d.title}` : `Created ${d.title}`)
      onDone()
    } catch (e) { notify((e as Error).message) } finally { setBusy(false) }
  }

  return (
    <form className="card form skill-form" onSubmit={(e) => { e.preventDefault(); void save() }}>
      <div className="form-grid">
        <label className="field-inline">
          <span className="slash-prefix">/</span>
          <input placeholder="name (e.g. data-pipeline)" value={d.name} required pattern="[a-z0-9][a-z0-9\-]{0,39}" autoFocus={!initial}
            title="Lowercase letters, digits and dashes"
            onChange={(e) => setD({ ...d, name: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, '') })} />
        </label>
        <input placeholder="Title (e.g. Data pipeline review)" value={d.title} onChange={set('title')} required maxLength={80} />
        <input className="span-2" required minLength={10} maxLength={300} value={d.description} onChange={set('description')}
          placeholder="When to use it — Qwen reads this to decide when to load the skill (e.g. Use when reviewing Airflow DAGs…)" />
        <textarea className="span-2 mono" rows={14} required minLength={20} maxLength={MAX_BODY} value={d.body} onChange={set('body')}
          placeholder={'The playbook Qwen follows, in Markdown: role, step-by-step workflow, checklists, the output format, and what to avoid.'} />
        <div className="span-2 muted small skill-count">{d.body.length.toLocaleString()} / {MAX_BODY.toLocaleString()} characters</div>
        {!initial && user?.role === 'admin' && (
          <select value={d.scope} onChange={set('scope')}>
            <option value="personal">Personal — only you</option>
            <option value="team">Team — everyone</option>
          </select>
        )}
      </div>
      <div className="row-end">
        <button type="button" className="btn ghost" onClick={onDone}>Cancel</button>
        <button className="btn primary" disabled={busy}>{busy ? <Loader2 size={14} className="spin" /> : initial ? 'Save' : 'Create skill'}</button>
      </div>
    </form>
  )
}

function SkillCard({ s, onEdit, onCopy }: { s: Skill; onEdit: () => void; onCopy: () => void }) {
  const { notify, refreshSkills } = useApp()
  const [open, setOpen] = useState(false)
  return (
    <div className={`card skill-card${open ? ' open' : ''}`}>
      <div className="skill-head">
        <span className={`skill-icon ${s.scope}`}>{s.scope === 'builtin' ? <Sparkles size={16} /> : <GraduationCap size={16} />}</span>
        <div className="skill-text">
          <div className="skill-title-row">
            <strong>{s.title}</strong>
            <code className="slash-cmd skill">/{s.name}</code>
            <span className="badge">{SCOPE_LABEL[s.scope]}</span>
          </div>
          <p className="muted small">{s.description}</p>
        </div>
        <div className="skill-actions">
          <button className="icon-btn sm" title="Duplicate and customise" onClick={onCopy}><Copy size={14} /></button>
          {s.editable ? (
            <>
              <button className="icon-btn sm" title="Edit" onClick={onEdit}><Pencil size={14} /></button>
              <button className="icon-btn sm danger" title="Delete" onClick={async () => {
                if (!confirm(`Delete the ${s.title} skill${s.scope === 'team' ? ' for the whole team' : ''}?`)) return
                try { await api.deleteSkill(s.id); await refreshSkills() } catch (e) { notify((e as Error).message) }
              }}><Trash2 size={14} /></button>
            </>
          ) : s.scope !== 'builtin' && <span className="muted small" title="Only admins can change team skills"><Lock size={12} /></span>}
          <button className="icon-btn sm" title={open ? 'Hide playbook' : 'Show playbook'} onClick={() => setOpen(!open)}>
            <ChevronDown size={16} className={open ? 'rot180' : ''} />
          </button>
        </div>
      </div>
      {open && <div className="skill-body"><Markdown text={s.body} /></div>}
    </div>
  )
}

export function SkillsView() {
  const { skills, user, refreshSkills } = useApp()
  const [editing, setEditing] = useState<string | 'new' | null>(null)
  const [copyOf, setCopyOf] = useState<Skill | null>(null)
  const [q, setQ] = useState('')
  useEffect(() => { void refreshSkills() }, [refreshSkills])

  const shown = useMemo(() => {
    const t = q.trim().toLowerCase()
    return t ? skills.filter((s) => `${s.name} ${s.title} ${s.description}`.toLowerCase().includes(t)) : skills
  }, [skills, q])
  const groups: [string, typeof UserIcon, Skill[]][] = [
    ['Your skills', UserIcon, shown.filter((s) => s.scope === 'personal')],
    ['Team skills', Users, shown.filter((s) => s.scope === 'team')],
    ['Built-in skills', Sparkles, shown.filter((s) => s.scope === 'builtin')],
  ]

  const startCopy = (s: Skill) => { setCopyOf(s); setEditing('new'); window.scrollTo({ top: 0, behavior: 'smooth' }) }
  const done = () => { setEditing(null); setCopyOf(null) }

  return (
    <div className="page">
      <div className="page-inner">
        <div className="page-head">
          <h1><GraduationCap size={22} className="accent-icon" /> Skills</h1>
          {editing !== 'new' && <button className="btn primary" onClick={() => { setCopyOf(null); setEditing('new') }}><Plus size={16} /> New skill</button>}
        </div>
        <p className="muted">
          Skills are expert playbooks — frontend, security audit, DevOps, architecture and more. Qwen loads the right one
          automatically when your request fits it, or you can pick one yourself: type <kbd>/</kbd> in a chat and choose it.
        </p>
        {editing === 'new' && <SkillForm key={copyOf?.id ?? 'new'} copyOf={copyOf ?? undefined} onDone={done} />}
        <div className="skills-search">
          <Search size={14} />
          <input placeholder="Search skills" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        {groups.map(([title, Icon, items]) => (items.length || (title === 'Your skills' && !q)) ? (
          <section key={title}>
            <h3 className="section-title"><Icon size={15} /> {title}
              {title === 'Team skills' && user?.role !== 'admin' && <span className="muted small"> · managed by admins</span>}</h3>
            {items.length ? items.map((s) => editing === s.id
              ? <SkillForm key={s.id} initial={s} onDone={done} />
              : <SkillCard key={s.id} s={s} onEdit={() => setEditing(s.id)} onCopy={() => startCopy(s)} />)
              : <p className="muted small">None yet. Write your own, or duplicate a built-in skill and adapt it to your team.</p>}
          </section>
        ) : null)}
      </div>
    </div>
  )
}
