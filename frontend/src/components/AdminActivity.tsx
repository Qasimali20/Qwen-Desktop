import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Loader2, Search } from 'lucide-react'
import { api } from '../api/client'
import type { ActivityEntry } from '../api/types'
import { useApp } from '../store/app'

const CATEGORIES: { id: string; label: string }[] = [
  { id: '', label: 'Everything' },
  { id: 'auth', label: 'Sign-ins' },
  { id: 'user', label: 'Users' },
  { id: 'api_key', label: 'API keys' },
  { id: 'connector', label: 'Connectors' },
  { id: 'skill', label: 'Team skills' },
  { id: 'shortcut', label: 'Team shortcuts' },
]

const FIELD: Record<string, string> = { display_name: 'name', disabled: 'disabled', role: 'role', label: 'name', url: 'URL', transport: 'transport', enabled: 'enabled', description: 'description' }

const fmtVal = (v: unknown) => (typeof v === 'boolean' ? (v ? 'yes' : 'no') : String(v ?? '—'))

function changesText(changes: unknown): string[] {
  if (!changes || typeof changes !== 'object') return []
  return Object.entries(changes as Record<string, [unknown, unknown]>).map(([k, [a, b]]) => `${FIELD[k] ?? k}: ${fmtVal(a)} → ${fmtVal(b)}`)
}

const list = (v: unknown) => (Array.isArray(v) ? v.join(', ') : String(v))

/** A plain-language sentence for an entry, plus detail lines. */
function describe(e: ActivityEntry): { text: string; details: string[]; alert?: boolean } {
  const t = e.target_label ?? ''
  const d = e.details
  switch (e.action) {
    case 'auth.login': return { text: 'signed in', details: [] }
    case 'auth.login_failed': return { text: 'failed to sign in', details: [String(d.reason ?? '')], alert: true }
    case 'auth.password_changed': return { text: 'changed their password', details: [] }
    case 'user.created': return { text: `created user ${t}`, details: [`role: ${d.role}`] }
    case 'user.updated': return {
      text: `changed user ${t}`,
      details: [...changesText(d.changes), ...(d.password_reset ? ['password reset (signed out everywhere)'] : [])],
    }
    case 'user.deleted': return {
      text: `deleted user ${t}`, alert: true,
      details: [`${d.display_name ?? ''} · ${d.role ?? ''}`, `${d.chats ?? 0} chats and ${d.projects ?? 0} projects removed`],
    }
    case 'api_key.revoked': return { text: `revoked API key ${t}`, details: d.prefix ? [`${d.prefix}…`] : [] }
    case 'connector.created': return {
      text: `added connector ${t}`,
      details: [String(d.url ?? ''), ...(d.headers ? [`headers: ${list(d.headers)}`] : []), ...(d.tools_off ? [`tools off: ${list(d.tools_off)}`] : [])],
    }
    case 'connector.updated': return {
      text: `changed connector ${t}`,
      details: [...changesText(d.changes), ...(d.headers_replaced ? [`replaced headers: ${list(d.headers_replaced)}`] : []),
        ...(d.tools_on ? [`tools on: ${list(d.tools_on)}`] : []), ...(d.tools_off ? [`tools off: ${list(d.tools_off)}`] : [])],
    }
    case 'connector.deleted': return { text: `removed connector ${t}`, details: [String(d.url ?? '')], alert: true }
    case 'skill.created': return { text: `created team skill ${t}`, details: [`/${d.name}`] }
    case 'skill.updated': return { text: `edited team skill ${t}`, details: d.fields ? [`changed: ${list(d.fields)}`] : [] }
    case 'skill.deleted': return { text: `deleted team skill ${t}`, details: [`/${d.name}`] }
    case 'shortcut.created': return { text: `created team shortcut ${t}`, details: [] }
    case 'shortcut.updated': return { text: `edited team shortcut ${t}`, details: d.fields ? [`changed: ${list(d.fields)}`] : [] }
    case 'shortcut.deleted': return { text: `deleted team shortcut ${t}`, details: [] }
    default: return { text: `${e.action}${t ? ` ${t}` : ''}`, details: [] }
  }
}

function when(ts: number): string {
  const d = new Date(ts * 1000)
  const today = new Date().toDateString() === d.toDateString()
  return today ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

export function ActivityTab() {
  const notify = useApp((s) => s.notify)
  const [category, setCategory] = useState('')
  const [q, setQ] = useState('')
  const [query, setQuery] = useState('')
  const [items, setItems] = useState<ActivityEntry[] | null>(null)
  const [next, setNext] = useState<number | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)

  // Debounce the search box.
  useEffect(() => {
    const t = setTimeout(() => setQuery(q.trim()), 300)
    return () => clearTimeout(t)
  }, [q])

  useEffect(() => {
    let live = true
    api.admin.activity({ category, q: query })
      .then((r) => { if (live) { setItems(r.items); setNext(r.next) } })
      .catch((e) => notify((e as Error).message))
    return () => { live = false }
  }, [category, query, notify])

  const more = useCallback(async () => {
    if (!next) return
    setLoadingMore(true)
    try {
      const r = await api.admin.activity({ category, q: query, before: next })
      setItems((cur) => [...(cur ?? []), ...r.items])
      setNext(r.next)
    } catch (e) { notify((e as Error).message) } finally { setLoadingMore(false) }
  }, [next, category, query, notify])

  return (
    <>
      <div className="section-head activity-head">
        <h3>Activity</h3>
        <div className="activity-filters">
          <select value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Kind of activity">
            {CATEGORIES.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
          </select>
          <label className="activity-search">
            <Search size={14} />
            <input placeholder="Person, target or IP" value={q} onChange={(e) => setQ(e.target.value)} />
          </label>
        </div>
      </div>
      <p className="muted small">Sign-ins, admin actions and team-wide changes, kept for a year. Passwords, tokens and keys are never recorded.</p>
      {!items ? <Loader2 className="spin" /> : (
        <div className="table-wrap admin-table">
          <table className="activity-table">
            <thead><tr><th>When</th><th>Who</th><th>What</th><th>From</th></tr></thead>
            <tbody>
              {items.map((e) => {
                const d = describe(e)
                return (
                  <tr key={e.id} className={d.alert ? 'alert' : ''}>
                    <td className="muted nowrap" title={new Date(e.at * 1000).toLocaleString()}>{when(e.at)}</td>
                    <td className="nowrap"><strong>{e.actor_name}</strong></td>
                    <td>
                      <span className="activity-text">{d.alert && <AlertTriangle size={13} />} {d.text}</span>
                      {d.details.filter(Boolean).map((x, i) => <div key={i} className="muted small">{x}</div>)}
                    </td>
                    <td className="muted mono small nowrap">{e.ip ?? '—'}</td>
                  </tr>
                )
              })}
              {!items.length && <tr><td colSpan={4} className="muted">Nothing recorded{category || query ? ' for this filter' : ' yet'}.</td></tr>}
            </tbody>
          </table>
        </div>
      )}
      {next && (
        <div className="row-center">
          <button className="btn ghost sm" disabled={loadingMore} onClick={() => void more()}>
            {loadingMore ? <Loader2 size={14} className="spin" /> : null} Show older
          </button>
        </div>
      )}
    </>
  )
}
