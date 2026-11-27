import { LogoMark } from './LogoMark'
import { useState, type FormEvent } from 'react'
import { Loader2 } from 'lucide-react'
import { useApp } from '../store/app'

export function Login() {
  const signIn = useApp((s) => s.signIn)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await signIn(username.trim(), password)
    } catch (err) {
      setError((err as Error).message)
      setPassword('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">
          <LogoMark />
          <span className="brand-text">
            <span className="brand-name">Qwen Desktop</span>
            <span className="brand-kicker">Sovereign AI Workspace</span>
          </span>
        </div>
        <h1>Sign in</h1>
        <label className="field">
          <span>Username</span>
          <input autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
        </label>
        <label className="field">
          <span>Password</span>
          <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && <div className="form-error">{error}</div>}
        <button className="btn primary block" disabled={busy || !username || !password}>
          {busy ? <Loader2 size={16} className="spin" /> : 'Continue'}
        </button>
        <p className="muted small center">Accounts are created by your administrator.</p>
      </form>
    </div>
  )
}
