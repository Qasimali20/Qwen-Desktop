import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, Loader2, RefreshCw, XCircle } from 'lucide-react'
import { api } from '../api/client'
import type { HealthCheck, HealthReport, HealthStatus } from '../api/types'

const REFRESH_MS = 30_000
const GROUPS = ['Models', 'Services', 'Server', 'Connectors', 'Security']

const SUMMARY: Record<HealthStatus, string> = {
  ok: 'Everything is working',
  warn: 'Working, with warnings',
  error: 'Something is down',
}

function StatusIcon({ status }: { status: HealthStatus }) {
  if (status === 'ok') return <CheckCircle2 size={16} className="hc-icon ok" aria-label="OK" />
  if (status === 'warn') return <AlertTriangle size={16} className="hc-icon warn" aria-label="Warning" />
  return <XCircle size={16} className="hc-icon error" aria-label="Error" />
}

function Row({ c }: { c: HealthCheck }) {
  return (
    <li className={`hc-row ${c.status}`}>
      <StatusIcon status={c.status} />
      <div className="hc-main">
        <div className="hc-label">{c.label}</div>
        <div className="hc-detail">{c.detail}</div>
      </div>
      {c.ms != null && <span className="hc-ms" title="Response time">{c.ms} ms</span>}
    </li>
  )
}

export function HealthTab() {
  const [report, setReport] = useState<HealthReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)

  const load = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    try {
      setReport(await api.admin.health())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load the health report')
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }, [])

  // Re-check every 30 s while the tab is open and visible.
  useEffect(() => {
    void load()
    const t = window.setInterval(() => { if (document.visibilityState === 'visible') void load() }, REFRESH_MS)
    return () => window.clearInterval(t)
  }, [load])

  if (!report && !error) return <Loader2 className="spin" />

  const groups = GROUPS.map((g) => ({ g, items: report?.checks.filter((c) => c.group === g) ?? [] }))
    .concat([{ g: 'Other', items: report?.checks.filter((c) => !GROUPS.includes(c.group)) ?? [] }])
    .filter((x) => x.items.length)
  const problems = report?.checks.filter((c) => c.status !== 'ok') ?? []

  return (
    <>
      <div className="section-head">
        <h3>Health</h3>
        <button className="btn" onClick={() => void load()} disabled={busy}>
          <RefreshCw size={14} className={busy ? 'spin' : ''} /> Check again
        </button>
      </div>
      {error && <p className="form-error">{error}</p>}
      {report && (
        <>
          <div className={`hc-banner ${report.status}`}>
            <StatusIcon status={report.status} />
            <div>
              <strong>{SUMMARY[report.status]}</strong>
              <span>
                {problems.length
                  ? `${problems.length} item${problems.length > 1 ? 's' : ''} need${problems.length > 1 ? '' : 's'} attention · `
                  : ''}
                checked {new Date(report.checked_at * 1000).toLocaleTimeString()} in {report.ms} ms · refreshes every 30 s
              </span>
            </div>
          </div>
          <div className="hc-grid">
            {groups.map(({ g, items }) => (
              <div className="card hc-card" key={g}>
                <h4 className="card-title">{g}</h4>
                <ul className="hc-list">
                  {/* Problems first within each group. */}
                  {[...items].sort((a, b) => rank(b.status) - rank(a.status)).map((c) => <Row key={c.id} c={c} />)}
                </ul>
              </div>
            ))}
          </div>
        </>
      )}
    </>
  )
}

const rank = (s: HealthStatus) => (s === 'error' ? 2 : s === 'warn' ? 1 : 0)
