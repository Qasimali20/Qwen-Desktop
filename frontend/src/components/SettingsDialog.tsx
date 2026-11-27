import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { api } from '../api/client'
import { useApp } from '../store/app'

function PasswordForm() {
  const notify = useApp((s) => s.notify)
  const [open, setOpen] = useState(false)
  const [cur, setCur] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  if (!open) return <button className="btn ghost sm" onClick={() => setOpen(true)}>Change password</button>
  return (
    <form className="pw-form" onSubmit={async (e) => {
      e.preventDefault()
      if (next !== confirm) { setError('New passwords do not match'); return }
      try {
        await api.changePassword(cur, next)
        notify('Password changed. Other devices were signed out.')
        setOpen(false); setCur(''); setNext(''); setConfirm(''); setError(null)
      } catch (err) { setError((err as Error).message) }
    }}>
      <input type="password" placeholder="Current password" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)} required />
      <input type="password" placeholder="New password (min 8)" autoComplete="new-password" minLength={8} value={next} onChange={(e) => setNext(e.target.value)} required />
      <input type="password" placeholder="Confirm new password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
      {error && <div className="form-error">{error}</div>}
      <div className="row-end">
        <button type="button" className="btn ghost sm" onClick={() => setOpen(false)}>Cancel</button>
        <button className="btn primary sm">Update password</button>
      </div>
    </form>
  )
}

export function SettingsDialog() {
  const { settingsOpen, setSettingsOpen, settings, saveSettings, config, user } = useApp()
  const [instructions, setInstructions] = useState('')

  useEffect(() => {
    if (settingsOpen) setInstructions(settings.custom_instructions ?? '')
  }, [settingsOpen, settings.custom_instructions])

  useEffect(() => {
    if (!settingsOpen) return
    const h = (e: KeyboardEvent) => e.key === 'Escape' && setSettingsOpen(false)
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [settingsOpen, setSettingsOpen])

  if (!settingsOpen) return null
  const close = () => {
    if (instructions !== (settings.custom_instructions ?? '')) void saveSettings({ custom_instructions: instructions })
    setSettingsOpen(false)
  }

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && close()}>
      <div className="modal" role="dialog" aria-label="Settings">
        <header><h2>Settings</h2><button className="icon-btn" onClick={close}><X size={18} /></button></header>

        <div className="account-row">
          <span className="initials lg">{user?.display_name.slice(0, 2).toUpperCase()}</span>
          <span className="account-text"><strong>{user?.display_name}</strong><span className="muted small">@{user?.username} · {user?.role}</span></span>
          <PasswordForm />
        </div>

        <label className="field">
          <span>Personal preferences</span>
          <small>Applied to every conversation — who you are, how you like answers.</small>
          <textarea rows={5} value={instructions} placeholder="e.g. I'm a CTO. Be concise, show code in TypeScript or Python, prefer tables for comparisons."
            onChange={(e) => setInstructions(e.target.value)} />
        </label>

        <div className="field-row">
          <label className="field">
            <span>Appearance</span>
            <select value={settings.theme ?? 'system'} onChange={(e) => void saveSettings({ theme: e.target.value as 'system' })}>
              <option value="system">Match system</option>
              <option value="light">Light</option>
              <option value="dark">Dark</option>
            </select>
          </label>
          <label className="field">
            <span>Voice</span>
            <select value={settings.voice_id ?? config?.voices[0]?.id} onChange={(e) => void saveSettings({ voice_id: e.target.value })}>
              {config?.voices.map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}
            </select>
          </label>
        </div>

        <label className="switch-field">
          <input type="checkbox" checked={!!settings.auto_speak} onChange={(e) => void saveSettings({ auto_speak: e.target.checked })} />
          <span>
            <strong>Voice mode</strong>
            <small>Dictated messages send automatically and replies are read aloud.</small>
          </span>
        </label>

        <div className="fleet">
          <span>Model fleet</span>
          {config?.models.map((m) => (
            <div key={m.id} className="fleet-row">
              <span className={`dot${m.online ? '' : ' off'}`} />
              <span>{m.label}</span>
              <span className="muted small">{m.online ? `${m.replicas}/${m.total} online · ${Math.round(m.context / 1000)}k ctx` : 'offline'}</span>
            </div>
          ))}
          <div className="muted small">
            Web search: {config?.features.web_search} · OCR: {config?.features.ocr ? 'PaddleOCR-VL' : 'off'} · ASR: {config?.features.asr ? 'Qwen3-ASR' : 'off'}
          </div>
        </div>
      </div>
    </div>
  )
}
