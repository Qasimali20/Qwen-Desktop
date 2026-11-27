import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, ChevronDown, Loader2, Pencil, Plug, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import { api, type ConnectorFields } from '../api/client'
import type { AdminConnector } from '../api/types'
import { useApp } from '../store/app'

type HeaderRow = { name: string; value: string }
type Form = { name: string; label: string; description: string; url: string; transport: 'http' | 'sse'; headers: HeaderRow[] }

const EMPTY: Form = { name: '', label: '', description: '', url: '', transport: 'http', headers: [{ name: 'Authorization', value: 'Bearer ' }] }

function headersOf(rows: HeaderRow[]): Record<string, string> {
  // A row with only the "Bearer " template left in it counts as empty.
  return Object.fromEntries(rows.filter((r) => r.name.trim() && r.value.trim() && r.value.trim() !== 'Bearer')
    .map((r) => [r.name.trim(), r.value.trim()]))
}

function ConnectorForm({ initial, onDone }: { initial?: AdminConnector; onDone: (c?: AdminConnector) => void }) {
  const notify = useApp((s) => s.notify)
  const [f, setF] = useState<Form>(() => initial
    ? { name: initial.name, label: initial.label, description: initial.description, url: initial.url, transport: initial.transport, headers: [] }
    : EMPTY)
  // Editing: headers are write-only. Leave them untouched unless the admin chooses to replace them.
  const [replaceHeaders, setReplaceHeaders] = useState(!initial)
  const [busy, setBusy] = useState(false)
  const set = (k: keyof Form) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value })
  const setRow = (i: number, k: keyof HeaderRow, v: string) => setF({ ...f, headers: f.headers.map((r, j) => (j === i ? { ...r, [k]: v } : r)) })

  const save = async () => {
    setBusy(true)
    try {
      const body: ConnectorFields = { label: f.label.trim(), description: f.description.trim(), url: f.url.trim(), transport: f.transport }
      if (replaceHeaders) body.headers = headersOf(f.headers)
      const c = initial ? await api.updateConnector(initial.id, body) : await api.createConnector({ ...body, name: f.name })
      notify(c.status === 'ok' ? `${c.label}: connected, ${c.tools.length} tools` : `${c.label}: ${c.error ?? 'saved'}`)
      onDone(c)
    } catch (e) { notify((e as Error).message) } finally { setBusy(false) }
  }

  return (
    <form className="card form connector-form" onSubmit={(e) => { e.preventDefault(); void save() }}>
      <div className="form-grid">
        <input placeholder="Name shown to people (e.g. Jira)" value={f.label} onChange={(e) => setF({
          ...f, label: e.target.value,
          ...(!initial && (f.name === '' || f.name === slug(f.label)) ? { name: slug(e.target.value) } : {}),
        })} required maxLength={60} autoFocus />
        <input placeholder="id (e.g. jira)" value={f.name} disabled={!!initial} required pattern="[a-z0-9][a-z0-9_]{0,23}"
          title="Lowercase letters, digits and underscores; used in tool names"
          onChange={(e) => setF({ ...f, name: e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '') })} />
        <input className="span-2" placeholder="MCP server URL (e.g. https://mcp.example.com/mcp)" value={f.url} onChange={set('url')} required type="url" />
        <select value={f.transport} onChange={set('transport')}>
          <option value="http">Streamable HTTP (current standard)</option>
          <option value="sse">SSE (older servers, URL usually ends in /sse)</option>
        </select>
        <input placeholder="What it's for (optional; shown to people and to Qwen)" value={f.description} onChange={set('description')} maxLength={300} />
        <div className="span-2 header-rows">
          <div className="muted small">
            Headers sent with every request — usually <code>Authorization: Bearer &lt;token&gt;</code>. Stored on the server and never shown again.
            {initial && (
              <label className="switch-inline"><input type="checkbox" checked={replaceHeaders} onChange={(e) => {
                setReplaceHeaders(e.target.checked)
                if (e.target.checked && !f.headers.length) setF({ ...f, headers: [{ name: 'Authorization', value: 'Bearer ' }] })
              }} /> Replace headers{initial.header_names.length ? ` (now: ${initial.header_names.join(', ')})` : ' (none set)'}</label>
            )}
          </div>
          {replaceHeaders && f.headers.map((r, i) => (
            <div key={i} className="header-row">
              <input className="mono" placeholder="Header" value={r.name} onChange={(e) => setRow(i, 'name', e.target.value)} />
              <input className="mono" placeholder="Value" type="password" autoComplete="off" value={r.value} onChange={(e) => setRow(i, 'value', e.target.value)} />
              <button type="button" className="icon-btn sm" aria-label="Remove header" onClick={() => setF({ ...f, headers: f.headers.filter((_, j) => j !== i) })}><X size={14} /></button>
            </div>
          ))}
          {replaceHeaders && <button type="button" className="btn ghost sm" onClick={() => setF({ ...f, headers: [...f.headers, { name: '', value: '' }] })}><Plus size={13} /> Header</button>}
        </div>
      </div>
      <div className="row-end">
        <button type="button" className="btn ghost" onClick={() => onDone()}>Cancel</button>
        <button className="btn primary" disabled={busy}>{busy ? <><Loader2 size={14} className="spin" /> Connecting…</> : initial ? 'Save' : 'Connect'}</button>
      </div>
    </form>
  )
}

const slug = (s: string) => s.toLowerCase().trim().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 24)

function ConnectorCard({ c, onChange, onDelete }: { c: AdminConnector; onChange: (c: AdminConnector) => void; onDelete: () => void }) {
  const notify = useApp((s) => s.notify)
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const act = async (fn: () => Promise<AdminConnector>) => {
    setBusy(true)
    try { onChange(await fn()) } catch (e) { notify((e as Error).message) } finally { setBusy(false) }
  }
  const toggleTool = (name: string, enabled: boolean) => act(() => api.updateConnector(c.id, {
    disabled_tools: c.tools.filter((t) => (t.name === name ? !enabled : !t.enabled)).map((t) => t.name),
  }))
  if (editing) return <ConnectorForm initial={c} onDone={(x) => { setEditing(false); if (x) onChange(x) }} />
  const on = c.tools.filter((t) => t.enabled).length
  return (
    <div className={`card connector-card${c.enabled ? '' : ' dim'}`}>
      <div className="connector-head">
        <span className={`dot${c.status === 'ok' ? '' : ' off'}`} title={c.status} />
        <div className="connector-text">
          <div><strong>{c.label}</strong> <code className="muted small">{c.name}</code> <span className="badge">{c.transport === 'sse' ? 'SSE' : 'HTTP'}</span></div>
          <div className="muted small mono connector-url">{c.url}</div>
          {c.description && <div className="muted small">{c.description}</div>}
        </div>
        <label className="switch-inline" title="Available to people in the composer">
          <input type="checkbox" checked={c.enabled} disabled={busy} onChange={(e) => void act(() => api.updateConnector(c.id, { enabled: e.target.checked }))} /> Enabled
        </label>
        <button className="icon-btn sm" title="Reconnect and refresh tools" disabled={busy} onClick={() => void act(() => api.refreshConnector(c.id))}>
          {busy ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}
        </button>
        <button className="icon-btn sm" title="Edit" onClick={() => setEditing(true)}><Pencil size={14} /></button>
        <button className="icon-btn sm danger" title="Delete" onClick={onDelete}><Trash2 size={14} /></button>
      </div>
      {c.status === 'error' && c.error && <p className="connector-error small"><AlertTriangle size={13} /> {c.error}</p>}
      <button className="connector-tools-toggle muted small" onClick={() => setOpen(!open)}>
        <ChevronDown size={14} className={open ? 'rot180' : ''} /> {on} of {c.tools.length} tools enabled
        {c.header_names.length ? ` · headers: ${c.header_names.join(', ')}` : ''}
      </button>
      {open && (
        <ul className="connector-tools">
          {c.tools.map((t) => (
            <li key={t.name}>
              <label>
                <input type="checkbox" checked={t.enabled} disabled={busy} onChange={(e) => void toggleTool(t.name, e.target.checked)} />
                <code>{t.name}</code>
                {t.read_only && <span className="badge">read-only</span>}
                {t.destructive && <span className="badge warn">changes data</span>}
              </label>
              {t.description && <p className="muted small">{t.description}</p>}
            </li>
          ))}
          {!c.tools.length && <li className="muted small">No tools reported by the server.</li>}
        </ul>
      )}
    </div>
  )
}

export function ConnectorsTab() {
  const { notify, refreshConnectors } = useApp()
  const [list, setList] = useState<AdminConnector[] | null>(null)
  const [adding, setAdding] = useState(false)
  const reload = useCallback(() => void api.adminConnectors().then(setList).catch((e) => notify((e as Error).message)), [notify])
  useEffect(reload, [reload])
  // Keep the composer's list in step with admin changes.
  const changed = (c: AdminConnector) => { setList((l) => l?.map((x) => (x.id === c.id ? c : x)) ?? null); void refreshConnectors() }

  if (!list) return <Loader2 className="spin" />
  return (
    <>
      <div className="section-head">
        <h3><Plug size={16} /> {list.length} connector{list.length === 1 ? '' : 's'}</h3>
        {!adding && <button className="btn primary sm" onClick={() => setAdding(true)}><Plus size={14} /> Add connector</button>}
      </div>
      <p className="muted small">
        Connectors are MCP servers (Model Context Protocol) — Jira, GitHub, databases, internal APIs. People switch them on
        under <em>Connectors</em> in the message box, and Qwen can then call their tools. Qwen asks before actions that change
        data; tools the server marks as changing data start switched off. Only add servers you trust: they receive what Qwen
        sends them.
      </p>
      {adding && <ConnectorForm onDone={(c) => { setAdding(false); if (c) { setList((l) => [...(l ?? []), c]); void refreshConnectors() } }} />}
      {list.map((c) => (
        <ConnectorCard key={c.id} c={c} onChange={changed} onDelete={async () => {
          if (!confirm(`Remove the ${c.label} connector for everyone?`)) return
          try { await api.deleteConnector(c.id); setList((l) => l?.filter((x) => x.id !== c.id) ?? null); void refreshConnectors() }
          catch (e) { notify((e as Error).message) }
        }} />
      ))}
      {!list.length && !adding && <p className="muted">No connectors yet.</p>}
    </>
  )
}
