import { useCallback, useEffect, useState } from 'react'
import { Check, Copy, KeyRound, Loader2, Plus, Trash2 } from 'lucide-react'
import { api } from '../api/client'
import type { ApiKey } from '../api/types'
import { relativeDay } from '../lib/text'
import { useApp } from '../store/app'
import { Markdown } from './Markdown'

function CopyInline({ text }: { text: string }) {
  const [done, setDone] = useState(false)
  return (
    <button className="icon-btn sm" title="Copy" onClick={() => {
      void navigator.clipboard.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500) })
    }}>{done ? <Check size={14} /> : <Copy size={14} />}</button>
  )
}

type Tool = 'continue' | 'qwen-code' | 'qwen-code-win' | 'claude-code' | 'curl' | 'python'

const TOOLS: { id: Tool; label: string }[] = [
  { id: 'qwen-code', label: 'Qwen Code (macOS / Linux)' },
  { id: 'qwen-code-win', label: 'Qwen Code (Windows)' },
  { id: 'continue', label: 'VS Code (Continue)' },
  { id: 'claude-code', label: 'Claude Code' },
  { id: 'python', label: 'Python (OpenAI SDK)' },
  { id: 'curl', label: 'curl' },
]

const QWEN_CODE_NOTE = 'Qwen Code is a coding agent for your terminal, like Claude Code: it reads your project, edits files and '
  + 'runs commands on your own computer, asking before each change (Shift+Tab switches to auto-accepting edits). '
  + 'It uses Qwen3 Coder on the team cluster through your key.'

function snippet(tool: Tool, base: string, key: string, model: string, coder: string, anthropicModel: string) {
  switch (tool) {
    case 'continue':
      return { lang: 'yaml', note: 'Install the Continue extension, then put this in ~/.continue/config.yaml:', code:
`name: Qwen Desktop
version: 1.0.0
schema: v1
models:
  - name: ${model}
    provider: openai
    model: ${model}
    apiBase: ${base}/v1
    apiKey: ${key}
    roles: [chat, edit, apply]
  - name: ${coder} (autocomplete)
    provider: openai
    model: ${coder}
    apiBase: ${base}/v1
    apiKey: ${key}
    roles: [autocomplete]` }
    case 'qwen-code':
      return { lang: 'bash', note: QWEN_CODE_NOTE, code:
`# 1. Install (needs Node.js 22 or newer)
npm install -g @qwen-code/qwen-code

# 2. Connect it to Qwen Desktop (one time; replaces ~/.qwen/settings.json if you have one)
mkdir -p ~/.qwen
cat > ~/.qwen/settings.json <<'JSON'
{
  "security": { "auth": { "selectedType": "openai" } },
  "model": { "name": "${coder}" }
}
JSON
cat > ~/.qwen/.env <<'ENV'
OPENAI_BASE_URL=${base}/v1
OPENAI_API_KEY=${key}
OPENAI_MODEL=${coder}
ENV
chmod 600 ~/.qwen/.env

# 3. Run it inside your project
cd ~/code/your-project
qwen` }
    case 'qwen-code-win':
      return { lang: 'powershell', note: QWEN_CODE_NOTE, code:
`# 1. Install (needs Node.js 22 or newer from nodejs.org)
npm install -g @qwen-code/qwen-code

# 2. Connect it to Qwen Desktop (one time; replaces .qwen\\settings.json in your home folder if you have one)
New-Item -ItemType Directory -Force "$HOME\\.qwen" | Out-Null
@'
{
  "security": { "auth": { "selectedType": "openai" } },
  "model": { "name": "${coder}" }
}
'@ | Set-Content -Encoding utf8 "$HOME\\.qwen\\settings.json"
@'
OPENAI_BASE_URL=${base}/v1
OPENAI_API_KEY=${key}
OPENAI_MODEL=${coder}
'@ | Set-Content -Encoding utf8 "$HOME\\.qwen\\.env"

# 3. Run it inside your project
cd C:\\code\\your-project
qwen` }
    case 'claude-code':
      return { lang: 'bash', note: `Claude Code speaks the Anthropic API, which the gateway serves too. Requests for claude-* models go to ${anthropicModel}.`, code:
`export ANTHROPIC_BASE_URL="${base}"
export ANTHROPIC_AUTH_TOKEN="${key}"
export ANTHROPIC_MODEL="${anthropicModel}"
export ANTHROPIC_SMALL_FAST_MODEL="${anthropicModel}"
claude` }
    case 'python':
      return { lang: 'python', note: 'pip install openai', code:
`from openai import OpenAI

client = OpenAI(base_url="${base}/v1", api_key="${key}")
reply = client.chat.completions.create(
    model="${model}",
    messages=[{"role": "user", "content": "Write a haiku about GPUs"}],
)
print(reply.choices[0].message.content)` }
    case 'curl':
      return { lang: 'bash', note: 'Any OpenAI-compatible client works the same way.', code:
`curl ${base}/v1/chat/completions \\
  -H "Authorization: Bearer ${key}" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${model}", "messages": [{"role": "user", "content": "Hello"}]}'` }
  }
}

export function DeveloperView() {
  const { config, notify } = useApp()
  const [keys, setKeys] = useState<ApiKey[] | null>(null)
  const [name, setName] = useState('')
  const [fresh, setFresh] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [tool, setTool] = useState<Tool>('qwen-code')

  const reload = useCallback(() => void api.keys().then(setKeys), [])
  useEffect(reload, [reload])

  const create = async () => {
    setBusy(true)
    try {
      const k = await api.createKey(name.trim() || 'My key')
      setFresh(k.key)
      setName('')
      reload()
    } catch (e) { notify((e as Error).message) } finally { setBusy(false) }
  }

  if (!config?.features.gateway) {
    return <div className="page"><div className="page-inner"><h1>API access</h1><p className="muted">The API gateway is disabled by your administrator.</p></div></div>
  }

  // Tools reject the self-signed certificate of the HTTPS port, so point them at plain HTTP.
  const base = (config.gateway.public_url ?? (location.protocol === 'https:'
    ? `http://${location.hostname}:${config.gateway.http_port}` : location.origin)).replace(/\/$/, '')
  const models = config.models
  const def = models.find((m) => m.default)?.id ?? models[0]?.id ?? ''
  const coder = models.find((m) => /coder/i.test(m.id))?.id ?? models.find((m) => !m.thinking && m.tools)?.id ?? def
  const s = snippet(tool, base, fresh ?? 'qd-YOUR-KEY', def, coder, config.gateway.anthropic_default_model)
  const active = keys?.filter((k) => !k.revoked_at) ?? []

  return (
    <div className="page">
      <div className="page-inner">
        <div className="page-head"><h1><KeyRound size={22} className="accent-icon" /> API keys &amp; IDE setup</h1></div>
        <p className="muted">
          Use the team's Qwen models from VS Code, the terminal or your own scripts. The endpoint is OpenAI- and
          Anthropic-compatible, and usage counts toward your account like chats do. Keep keys secret: anyone with a key
          can use it as you.
        </p>

        <div className="card endpoint-card">
          <div><span className="muted small">Base URL</span><code>{base}/v1</code><CopyInline text={`${base}/v1`} /></div>
          <div><span className="muted small">Models</span>
            <span className="model-ids">{models.map((m) => <code key={m.id} className={m.online ? '' : 'off'} title={m.online ? m.description : 'offline'}>{m.id}</code>)}</span>
          </div>
        </div>

        <h3 className="section-title">Your keys</h3>
        {fresh && (
          <div className="card new-key">
            <strong>Copy your new key now. It won't be shown again.</strong>
            <div className="key-line"><code>{fresh}</code><CopyInline text={fresh} /></div>
            <button className="btn ghost sm" onClick={() => setFresh(null)}>Done</button>
          </div>
        )}
        <form className="key-create" onSubmit={(e) => { e.preventDefault(); void create() }}>
          <input placeholder="Key name, e.g. work laptop" value={name} onChange={(e) => setName(e.target.value)} maxLength={60} />
          <button className="btn primary sm" disabled={busy}>{busy ? <Loader2 size={14} className="spin" /> : <><Plus size={14} /> Create key</>}</button>
        </form>
        {keys === null ? <Loader2 className="spin" /> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Name</th><th>Key</th><th>Created</th><th>Last used</th><th /></tr></thead>
              <tbody>
                {keys.map((k) => (
                  <tr key={k.id} className={k.revoked_at ? 'dim' : ''}>
                    <td>{k.name}</td>
                    <td><code>{k.prefix}…</code></td>
                    <td className="muted">{relativeDay(k.created_at)}</td>
                    <td className="muted">{k.revoked_at ? 'Revoked' : k.last_used_at ? relativeDay(k.last_used_at) : 'Never'}</td>
                    <td className="row-actions">
                      {!k.revoked_at && (
                        <button className="icon-btn sm danger" title="Revoke" onClick={async () => {
                          if (!confirm(`Revoke “${k.name}”? Tools using it will stop working immediately.`)) return
                          await api.revokeKey(k.id)
                          reload()
                        }}><Trash2 size={14} /></button>
                      )}
                    </td>
                  </tr>
                ))}
                {!keys.length && <tr><td colSpan={5} className="muted">No keys yet.</td></tr>}
              </tbody>
            </table>
          </div>
        )}
        {active.length > 0 && <p className="muted small">{active.length} active key{active.length > 1 ? 's' : ''}.</p>}

        <h3 className="section-title">Set up a tool</h3>
        <div className="tabs">
          {TOOLS.map((t) => <button key={t.id} className={tool === t.id ? 'on' : ''} onClick={() => setTool(t.id)}>{t.label}</button>)}
        </div>
        <p className="muted small">{s.note}{!fresh && ' Replace qd-YOUR-KEY with your key.'}</p>
        <Markdown text={`\`\`\`${s.lang}\n${s.code}\n\`\`\``} />
      </div>
    </div>
  )
}
