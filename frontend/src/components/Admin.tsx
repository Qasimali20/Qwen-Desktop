import { useCallback, useEffect, useMemo, useState } from 'react'
import { KeyRound, Loader2, Plus, ShieldCheck, Trash2, UserX, UserCheck } from 'lucide-react'
import { api } from '../api/client'
import type { AdminUser, ApiKey, Usage } from '../api/types'
import { useApp } from '../store/app'
import { ActivityTab } from './AdminActivity'
import { ConnectorsTab } from './AdminConnectors'
import { HealthTab } from './AdminHealth'

const fmt = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : `${n}`)

function ago(ts: number | null): string {
  if (!ts) return 'never'
  const s = Date.now() / 1000 - ts
  if (s < 120) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  return `${Math.floor(s / 86400)} d ago`
}

function NewUserForm({ onDone }: { onDone: () => void }) {
  const notify = useApp((s) => s.notify)
  const [f, setF] = useState({ username: '', display_name: '', password: '', role: 'user' as 'user' | 'admin' })
  const [busy, setBusy] = useState(false)
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value })
  const generate = () => {
    const bytes = crypto.getRandomValues(new Uint8Array(12))
    setF({ ...f, password: btoa(String.fromCharCode(...bytes)).replace(/[+/=]/g, '').slice(0, 14) })
  }
  return (
    <form className="card form" onSubmit={async (e) => {
      e.preventDefault()
      setBusy(true)
      try {
        await api.admin.createUser({ ...f, display_name: f.display_name || f.username })
        notify(`Created ${f.username}. Share the password with them securely.`)
        onDone()
      } catch (err) { notify((err as Error).message) } finally { setBusy(false) }
    }}>
      <div className="form-grid">
        <input placeholder="Username (e.g. a.khan)" value={f.username} onChange={set('username')} pattern="[A-Za-z0-9._-]{2,32}" required autoFocus />
        <input placeholder="Full name" value={f.display_name} onChange={set('display_name')} />
        <div className="pw-row">
          <input placeholder="Initial password (min 8)" value={f.password} onChange={set('password')} minLength={8} required />
          <button type="button" className="btn ghost sm" onClick={generate}>Generate</button>
        </div>
        <select value={f.role} onChange={set('role')}>
          <option value="user">User</option>
          <option value="admin">Admin</option>
        </select>
      </div>
      <div className="row-end">
        <button type="button" className="btn ghost" onClick={onDone}>Cancel</button>
        <button className="btn primary" disabled={busy}>{busy ? <Loader2 size={14} className="spin" /> : 'Create user'}</button>
      </div>
    </form>
  )
}

function UsersTab({ users, usage, reload }: { users: AdminUser[]; usage: Usage | null; reload: () => void }) {
  const { user: me, notify } = useApp()
  const [creating, setCreating] = useState(false)
  const byUser = useMemo(() => new Map(usage?.by_user.map((u) => [u.user_id, u])), [usage])

  const act = async (fn: () => Promise<unknown>, msg?: string) => {
    try { await fn(); if (msg) notify(msg); reload() } catch (e) { notify((e as Error).message) }
  }

  return (
    <>
      <div className="section-head">
        <h3>{users.length} users</h3>
        {!creating && <button className="btn primary sm" onClick={() => setCreating(true)}><Plus size={14} /> Add user</button>}
      </div>
      {creating && <NewUserForm onDone={() => { setCreating(false); reload() }} />}
      <div className="table-wrap admin-table">
        <table>
          <thead>
            <tr><th>User</th><th>Role</th><th>Status</th><th>Chats</th><th>Tokens ({usage?.days ?? 30}d)</th><th>Last active</th><th /></tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const t = byUser.get(u.id)
              const self = u.id === me?.id
              return (
                <tr key={u.id} className={u.disabled ? 'dim' : ''}>
                  <td><div className="user-cell"><span className="initials">{u.display_name.slice(0, 2).toUpperCase()}</span>
                    <span><strong>{u.display_name}</strong><span className="muted small">@{u.username}{self ? ' · you' : ''}</span></span></div></td>
                  <td>{u.role === 'admin' ? <span className="badge accent"><ShieldCheck size={12} /> Admin</span> : <span className="badge">User</span>}</td>
                  <td>{u.disabled ? <span className="badge danger">Disabled</span> : <span className="badge ok">Active</span>}</td>
                  <td>{u.chat_count}</td>
                  <td>{t ? `${fmt(t.prompt_tokens)} in · ${fmt(t.completion_tokens)} out` : '—'}</td>
                  <td className="muted">{ago(u.last_seen_at ?? u.last_login_at)}</td>
                  <td className="row-actions">
                    <button className="icon-btn sm" title="Reset password" onClick={() => {
                      const pw = prompt(`New password for ${u.username} (min 8 characters):`)
                      if (pw) void act(() => api.admin.updateUser(u.id, { password: pw }), 'Password reset; their sessions were signed out.')
                    }}><KeyRound size={14} /></button>
                    {!self && (
                      <>
                        <button className="icon-btn sm" title={u.role === 'admin' ? 'Make user' : 'Make admin'}
                          onClick={() => void act(() => api.admin.updateUser(u.id, { role: u.role === 'admin' ? 'user' : 'admin' }))}>
                          <ShieldCheck size={14} />
                        </button>
                        <button className="icon-btn sm" title={u.disabled ? 'Enable' : 'Disable'}
                          onClick={() => void act(() => api.admin.updateUser(u.id, { disabled: !u.disabled }))}>
                          {u.disabled ? <UserCheck size={14} /> : <UserX size={14} />}
                        </button>
                        <button className="icon-btn sm danger" title="Delete user and all their data" onClick={() => {
                          if (confirm(`Delete ${u.username} and ALL their chats, projects and files? This cannot be undone.`)) {
                            void act(() => api.admin.deleteUser(u.id), 'User deleted')
                          }
                        }}><Trash2 size={14} /></button>
                      </>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </>
  )
}

function UsageTab({ usage, users, days, setDays }: { usage: Usage | null; users: AdminUser[]; days: number; setDays: (d: number) => void }) {
  const models = useApp((s) => s.config?.models ?? [])
  if (!usage) return <Loader2 className="spin" />
  const total = usage.by_model.reduce((a, m) => ({
    replies: a.replies + m.replies, prompt: a.prompt + m.prompt_tokens, completion: a.completion + m.completion_tokens,
  }), { replies: 0, prompt: 0, completion: 0 })
  const maxDay = Math.max(1, ...usage.by_day.map((d) => d.prompt_tokens + d.completion_tokens))
  const name = (uid: string) => users.find((u) => u.id === uid)?.display_name ?? 'Deleted user'
  const label = (mid: string | null) => models.find((m) => m.id === mid)?.label ?? mid ?? 'unknown'

  return (
    <>
      <div className="section-head">
        <h3>Usage</h3>
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
          <option value={7}>Last 7 days</option><option value={30}>Last 30 days</option><option value={90}>Last 90 days</option>
        </select>
      </div>
      <div className="stat-row">
        <div className="card stat"><span>Replies</span><strong>{fmt(total.replies)}</strong></div>
        <div className="card stat"><span>Input tokens</span><strong>{fmt(total.prompt)}</strong></div>
        <div className="card stat"><span>Output tokens</span><strong>{fmt(total.completion)}</strong></div>
        <div className="card stat"><span>Active users</span><strong>{usage.by_user.length}</strong></div>
      </div>
      {(() => {
        const src = (k: 'app' | 'api') => usage.by_source.find((x) => x.source === k)
        const tokens = (r?: { prompt_tokens: number; completion_tokens: number }) => (r ? r.prompt_tokens + r.completion_tokens : 0)
        const app = src('app'), apiRow = src('api')
        const all = Math.max(1, tokens(app) + tokens(apiRow))
        return (
          <div className="card source-split">
            <h4 className="card-title">Where usage comes from</h4>
            <div className="split-bar">
              <span style={{ width: `${(tokens(app) / all) * 100}%` }} />
              <span className="api" style={{ width: `${(tokens(apiRow) / all) * 100}%` }} />
            </div>
            <div className="split-legend">
              <span><i /> Chat app · {app?.replies ?? 0} replies · {fmt(tokens(app))} tok</span>
              <span><i className="api" /> API / IDEs · {apiRow?.replies ?? 0} requests · {fmt(tokens(apiRow))} tok</span>
            </div>
          </div>
        )
      })()}
      <div className="card">
        <h4 className="card-title">Tokens per day</h4>
        {usage.by_day.length ? (
          <div className="bars">
            {usage.by_day.map((d) => {
              const v = d.prompt_tokens + d.completion_tokens
              return (
                <div key={d.day} className="bar-col" title={`${d.day}: ${fmt(v)} tokens, ${d.replies} replies`}>
                  <div className="bar" style={{ height: `${(v / maxDay) * 100}%` }} />
                  <span>{d.day.slice(5)}</span>
                </div>
              )
            })}
          </div>
        ) : <p className="muted">No activity in this period.</p>}
      </div>
      <div className="two-col">
        <div className="card">
          <h4 className="card-title">By model</h4>
          <table className="mini"><tbody>
            {usage.by_model.map((m) => <tr key={m.model_id ?? 'x'}><td>{label(m.model_id)}</td><td>{m.replies} replies</td><td>{fmt(m.prompt_tokens + m.completion_tokens)} tok</td></tr>)}
          </tbody></table>
        </div>
        <div className="card">
          <h4 className="card-title">By user</h4>
          <table className="mini"><tbody>
            {[...usage.by_user].sort((a, b) => b.completion_tokens - a.completion_tokens).map((u) =>
              <tr key={u.user_id}><td>{name(u.user_id)}</td><td>{u.replies} replies</td><td>{fmt(u.prompt_tokens + u.completion_tokens)} tok</td></tr>)}
          </tbody></table>
        </div>
      </div>
    </>
  )
}

function KeysTab() {
  const notify = useApp((s) => s.notify)
  const [keys, setKeys] = useState<ApiKey[] | null>(null)
  const reload = useCallback(() => void api.admin.keys().then(setKeys), [])
  useEffect(reload, [reload])
  if (!keys) return <Loader2 className="spin" />
  const active = keys.filter((k) => !k.revoked_at)
  return (
    <>
      <div className="section-head"><h3>{active.length} active API keys</h3></div>
      <p className="muted small">People create their own keys under <em>API keys &amp; IDE setup</em>. Disabling a user also blocks their keys.</p>
      <div className="table-wrap admin-table">
        <table>
          <thead><tr><th>Owner</th><th>Name</th><th>Key</th><th>Created</th><th>Last used</th><th /></tr></thead>
          <tbody>
            {keys.map((k) => (
              <tr key={k.id} className={k.revoked_at ? 'dim' : ''}>
                <td><strong>{k.display_name}</strong> <span className="muted small">@{k.username}</span></td>
                <td>{k.name}</td>
                <td><code>{k.prefix}…</code></td>
                <td className="muted">{ago(k.created_at)}</td>
                <td className="muted">{k.revoked_at ? 'Revoked' : ago(k.last_used_at)}</td>
                <td className="row-actions">
                  {!k.revoked_at && (
                    <button className="icon-btn sm danger" title="Revoke" onClick={async () => {
                      if (!confirm(`Revoke ${k.display_name}'s key “${k.name}”?`)) return
                      try { await api.admin.revokeKey(k.id); reload() } catch (e) { notify((e as Error).message) }
                    }}><Trash2 size={14} /></button>
                  )}
                </td>
              </tr>
            ))}
            {!keys.length && <tr><td colSpan={6} className="muted">No API keys yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  )
}

export function AdminView() {
  const { user, navigate } = useApp()
  const [tab, setTab] = useState<'users' | 'health' | 'usage' | 'keys' | 'connectors' | 'activity'>('users')
  const [users, setUsers] = useState<AdminUser[]>([])
  const [usage, setUsage] = useState<Usage | null>(null)
  const [days, setDays] = useState(30)

  const reload = useCallback(() => {
    void api.admin.users().then(setUsers)
    void api.admin.usage(days).then(setUsage)
  }, [days])

  useEffect(() => {
    if (user?.role !== 'admin') { navigate({ name: 'home' }, true); return }
    reload()
  }, [user, navigate, reload])

  return (
    <div className="page">
      <div className="page-inner wide">
        <div className="page-head"><h1>Admin</h1></div>
        <div className="tabs">
          <button className={tab === 'users' ? 'on' : ''} onClick={() => setTab('users')}>Users</button>
          <button className={tab === 'health' ? 'on' : ''} onClick={() => setTab('health')}>Health</button>
          <button className={tab === 'usage' ? 'on' : ''} onClick={() => setTab('usage')}>Usage</button>
          <button className={tab === 'keys' ? 'on' : ''} onClick={() => setTab('keys')}>API keys</button>
          <button className={tab === 'connectors' ? 'on' : ''} onClick={() => setTab('connectors')}>Connectors</button>
          <button className={tab === 'activity' ? 'on' : ''} onClick={() => setTab('activity')}>Activity</button>
        </div>
        {tab === 'users' && <UsersTab users={users} usage={usage} reload={reload} />}
        {tab === 'health' && <HealthTab />}
        {tab === 'usage' && <UsageTab usage={usage} users={users} days={days} setDays={setDays} />}
        {tab === 'keys' && <KeysTab />}
        {tab === 'connectors' && <ConnectorsTab />}
        {tab === 'activity' && <ActivityTab />}
      </div>
    </div>
  )
}
