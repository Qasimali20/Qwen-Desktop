import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowUp, BarChart3, Brain, Check, ChevronDown, Eye, Globe, GraduationCap, Loader2, Mic, Paperclip, Plug, Settings2, ShieldAlert, Square, Users, Wrench, X, Zap } from 'lucide-react'
import { api } from '../api/client'
import type { Attachment, ModelInfo, PromptShortcut, Skill } from '../api/types'
import { Recorder } from '../lib/audio'
import { describeFindings, findSecrets, redactSecrets, type SecretFinding } from '../lib/secrets'
import { useApp } from '../store/app'
import { AttachmentChip } from './Message'

type Pending = { key: string; file: File; att?: Attachment }

export interface SendPayload {
  content: string
  attachments: Attachment[]
  thinking: boolean
  webSearch: boolean
  analysis: boolean
  prompt: PromptShortcut | null
  skill: Skill | null
}

interface Props {
  modelId: string | undefined
  onModelChange: (id: string) => void
  onSend: (p: SendPayload) => void
  onStop: () => void
  streaming: boolean
  placeholder?: string
  autoFocus?: boolean
}

function ModelPicker({ value, onChange }: { value?: string; onChange: (id: string) => void }) {
  const models = useApp((s) => s.config?.models ?? [])
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const current = models.find((m) => m.id === value)

  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false)
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])

  const caps = (m: ModelInfo) => (
    <span className="caps">
      {m.thinking && <Brain size={12} aria-label="thinking" />}
      {m.tools && <Wrench size={12} aria-label="tools" />}
      {m.vision && <Eye size={12} aria-label="vision" />}
    </span>
  )

  return (
    <div className="model-picker" ref={ref}>
      <button className="model-btn" onClick={() => setOpen(!open)}>
        <span className={`dot${current?.online === false ? ' off' : ''}`} />
        {current?.label ?? 'Select model'}
        <ChevronDown size={14} />
      </button>
      {open && (
        <div className="menu model-menu">
          {models.map((m) => (
            <button key={m.id} className="menu-item" disabled={!m.online} onClick={() => { onChange(m.id); setOpen(false) }}>
              <span className={`dot${m.online ? '' : ' off'}`} />
              <span className="mm-text">
                <span className="mm-label">{m.label} {caps(m)}</span>
                <span className="mm-desc">
                  {m.online ? m.description : 'Offline'}
                  {m.online && m.total && m.total > 1 ? ` · ${m.replicas}/${m.total} replicas` : ''}
                </span>
              </span>
              {m.id === value && <Check size={16} />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/** One entry per command: a personal shortcut shadows the team one with the same command. */
function useShortcuts(): PromptShortcut[] {
  const prompts = useApp((s) => s.prompts)
  return useMemo(() => {
    const by = new Map<string, PromptShortcut>()
    for (const p of prompts) if (!by.has(p.command) || p.scope === 'personal') by.set(p.command, p)
    return [...by.values()].sort((a, b) => a.command.localeCompare(b.command))
  }, [prompts])
}

/** An entry of the "/" picker: a prompt shortcut or a skill. */
type SlashItem = { kind: 'prompt'; key: string; command: string; title: string; description: string; p: PromptShortcut }
  | { kind: 'skill'; key: string; command: string; title: string; description: string; s: Skill }

function SlashMenu({ items, active, onPick, onHover, onManage }: {
  items: SlashItem[]; active: number; onPick: (it: SlashItem) => void; onHover: (i: number) => void
  onManage: (what: 'shortcuts' | 'skills') => void
}) {
  const list = useRef<HTMLDivElement>(null)
  useEffect(() => {
    list.current?.querySelector<HTMLElement>(`[data-i="${active}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [active])
  return (
    <div className="menu slash-menu" role="listbox" onMouseDown={(e) => e.preventDefault()}>
      <div className="slash-list" ref={list}>
        {items.length ? items.map((it, i) => (
          <div key={it.key}>
            {(i === 0 || items[i - 1].kind !== it.kind) && (
              <div className="slash-group">{it.kind === 'skill' ? 'Skills' : 'Shortcuts'}</div>
            )}
            <button data-i={i} role="option" aria-selected={i === active}
              className={`menu-item slash-item${i === active ? ' active' : ''}`}
              onMouseEnter={() => onHover(i)} onClick={() => onPick(it)}>
              <span className={`slash-cmd${it.kind === 'skill' ? ' skill' : ''}`}>/{it.command}</span>
              <span className="slash-text">
                <span className="slash-title">{it.title}</span>
                {it.description && <span className="slash-desc">{it.description}</span>}
              </span>
              {it.kind === 'prompt' && it.p.scope === 'team' ? <Users size={13} className="muted" aria-label="Team shortcut" />
                : it.kind === 'skill' ? <GraduationCap size={13} className="muted" aria-label="Skill" /> : null}
            </button>
          </div>
        )) : <div className="slash-empty">No matching shortcut or skill</div>}
      </div>
      <div className="slash-manage-row">
        <button className="slash-manage" onClick={() => onManage('shortcuts')}><Settings2 size={13} /> Manage shortcuts</button>
        <button className="slash-manage" onClick={() => onManage('skills')}><GraduationCap size={13} /> Manage skills</button>
      </div>
    </div>
  )
}

/** Composer toggle for MCP connectors: which connected services Qwen may use in chats. */
function ConnectorsMenu() {
  const { connectors, settings, saveSettings, user, navigate } = useApp()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false)
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])
  if (!connectors.length) return null
  const on = new Set(settings.connectors ?? [])
  const active = connectors.filter((c) => on.has(c.id))
  const toggle = (id: string) => {
    const next = new Set(on)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    void saveSettings({ connectors: [...next] })
  }
  return (
    <div className="connectors-picker" ref={ref}>
      <button className={`toggle${active.length ? ' on' : ''}`} title="Connected services (MCP) Qwen may use" onClick={() => setOpen(!open)}>
        <Plug size={15} /> <span>{active.length ? `${active.length} connector${active.length > 1 ? 's' : ''}` : 'Connectors'}</span>
      </button>
      {open && (
        <div className="menu connectors-menu">
          <div className="menu-head muted small">Let Qwen use these services in your chats</div>
          {connectors.map((c) => (
            <label key={c.id} className="menu-item connector-item">
              <input type="checkbox" checked={on.has(c.id)} onChange={() => toggle(c.id)} />
              <span className="mm-text">
                <span className="mm-label">{c.label} <span className="muted small">{c.tool_count} tool{c.tool_count === 1 ? '' : 's'}</span></span>
                {c.description && <span className="mm-desc">{c.description}</span>}
              </span>
              {c.status === 'error' && <span className="badge warn" title="The last connection check failed">offline?</span>}
            </label>
          ))}
          {user?.role === 'admin' && (
            <button className="slash-manage" onClick={() => { setOpen(false); navigate({ name: 'admin' }) }}><Settings2 size={13} /> Manage connectors</button>
          )}
        </div>
      )}
    </div>
  )
}

export function Composer({ modelId, onModelChange, onSend, onStop, streaming, placeholder, autoFocus }: Props) {
  const { config, settings, saveSettings, notify, navigate } = useApp()
  const shortcuts = useShortcuts()
  const skills = useApp((s) => s.skills)
  const [text, setText] = useState('')
  const [prompt, setPrompt] = useState<PromptShortcut | null>(null)
  const [skill, setSkill] = useState<Skill | null>(null)
  const [slashIndex, setSlashIndex] = useState(0)
  const [slashDismissed, setSlashDismissed] = useState(false)
  const [files, setFiles] = useState<Pending[]>([])
  const [dragging, setDragging] = useState(false)
  const [mic, setMic] = useState<'idle' | 'recording' | 'transcribing'>('idle')
  const [level, setLevel] = useState(0)
  // Credentials spotted in the message about to be sent: the user chooses to redact, send or edit.
  const [secretWarn, setSecretWarn] = useState<{ content: string; findings: SecretFinding[] } | null>(null)
  const ta = useRef<HTMLTextAreaElement>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const recorder = useRef<Recorder | null>(null)

  const model = config?.models.find((m) => m.id === modelId)
  const thinking = !!settings.thinking && !!model?.thinking
  const webSearch = !!settings.web_search && !!model?.tools
  const analysisAvailable = !!model?.tools && !!config?.features.analysis
  const analysis = settings.analysis !== false && analysisAvailable
  const uploading = files.some((f) => !f.att)
  const canSend = !streaming && !uploading && (text.trim().length > 0 || files.length > 0 || !!prompt || !!skill)

  // "/" at the start of the composer opens the picker (shortcuts, then skills), filtered as you type.
  const slashQuery = !slashDismissed ? /^\/([a-z0-9-]*)$/i.exec(text)?.[1]?.toLowerCase() : undefined
  const allItems = useMemo<SlashItem[]>(() => [
    ...(prompt ? [] : shortcuts.map((p) => ({ kind: 'prompt' as const, key: `p:${p.id}`, command: p.command, title: p.title, description: p.description, p }))),
    ...(skill ? [] : skills.map((s) => ({ kind: 'skill' as const, key: `s:${s.id}`, command: s.name, title: s.title, description: s.description, s }))),
  ], [shortcuts, skills, prompt, skill])
  const slashItems = useMemo(() => {
    if (slashQuery === undefined) return []
    const rank = (it: SlashItem) => it.command.startsWith(slashQuery) ? 0
      : it.command.includes(slashQuery) || it.title.toLowerCase().includes(slashQuery) ? 1 : 2
    // Keep the two groups together (shortcuts first), best matches first within each.
    return allItems.filter((it) => rank(it) < 2)
      .sort((a, b) => (a.kind === b.kind ? 0 : a.kind === 'prompt' ? -1 : 1) || rank(a) - rank(b))
  }, [allItems, slashQuery])
  const slashOpen = slashQuery !== undefined
  const slashActive = Math.min(slashIndex, Math.max(0, slashItems.length - 1))

  const pick = (it: SlashItem, rest = '') => {
    if (it.kind === 'prompt') setPrompt(it.p)
    else setSkill(it.s)
    setText(rest)
    ta.current?.focus()
  }

  const onTextChange = (value: string) => {
    setSecretWarn(null)
    if (!value.startsWith('/')) setSlashDismissed(false)
    setSlashIndex(0)
    // Typing a full command followed by a space ("/review …") selects it directly.
    const m = /^\/([a-z0-9-]+)\s([\s\S]*)$/i.exec(value)
    const hit = m && allItems.find((it) => it.command === m[1].toLowerCase())
    if (m && hit) pick(hit, m[2])
    else setText(value)
  }

  // Auto-grow the textarea up to a cap.
  useEffect(() => {
    const el = ta.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 320)}px`
  }, [text])

  const addFiles = useCallback((list: FileList | File[]) => {
    const items = Array.from(list).map((file) => ({ key: `${file.name}-${file.size}-${Math.random()}`, file }))
    if (!items.length) return
    setFiles((f) => [...f, ...items])
    for (const item of items) {
      api.upload(item.file)
        .then((att) => setFiles((f) => f.map((x) => (x.key === item.key ? { ...x, att } : x))))
        .catch((e) => {
          notify(`${item.file.name}: ${(e as Error).message}`)
          setFiles((f) => f.filter((x) => x.key !== item.key))
        })
    }
  }, [notify])

  const submit = useCallback((override?: string, checked = false) => {
    const content = (override ?? text).trim()
    if (streaming || uploading || (!content && !files.length && !prompt && !skill)) return
    if (!checked) {
      const findings = findSecrets(content)
      if (findings.length) {
        if (override !== undefined) setText(content) // dictated text: keep it so it can be reviewed
        setSecretWarn({ content, findings })
        return
      }
    }
    setSecretWarn(null)
    onSend({ content, attachments: files.map((f) => f.att!), thinking, webSearch, analysis, prompt, skill })
    setText('')
    setFiles([])
    setPrompt(null)
    setSkill(null)
  }, [text, files, streaming, uploading, onSend, thinking, webSearch, analysis, prompt, skill])

  // ---- voice input
  useEffect(() => {
    if (mic !== 'recording') return
    let raf = 0
    const tick = () => { setLevel(recorder.current?.level() ?? 0); raf = requestAnimationFrame(tick) }
    tick()
    return () => cancelAnimationFrame(raf)
  }, [mic])

  useEffect(() => () => recorder.current?.cancel(), [])

  const toggleMic = async () => {
    if (mic === 'transcribing') return
    if (mic === 'idle') {
      try {
        recorder.current = new Recorder()
        await recorder.current.start()
        setMic('recording')
      } catch (e) {
        notify((e as Error).message)
      }
      return
    }
    setMic('transcribing')
    try {
      const wav = await recorder.current!.stop()
      const said = await api.transcribe(wav)
      if (!said) notify('No speech detected')
      else if (settings.auto_speak) submit(said) // voice mode: talk → send
      else setText((t) => (t ? `${t} ${said}` : said))
    } catch (e) {
      notify(`Transcription failed: ${(e as Error).message}`)
    } finally {
      setMic('idle')
      ta.current?.focus()
    }
  }

  return (
    <div
      className={`composer${dragging ? ' dragging' : ''}`}
      onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => { e.preventDefault(); setDragging(false); addFiles(e.dataTransfer.files) }}
    >
      {slashOpen && (
        <SlashMenu items={slashItems} active={slashActive} onHover={setSlashIndex}
          onPick={(it) => pick(it)} onManage={(what) => navigate({ name: what })} />
      )}
      {(prompt || skill) && (
        <div className="prompt-chip-row">
          {skill && (
            <span className="prompt-chip skill" title={skill.description}>
              <GraduationCap size={13} /> <strong>{skill.title}</strong> <span>skill</span>
              <button aria-label="Remove skill" onClick={() => { setSkill(null); ta.current?.focus() }}><X size={12} /></button>
            </span>
          )}
          {prompt && (
            <span className="prompt-chip" title={prompt.description || prompt.title}>
              <Zap size={13} /> <strong>/{prompt.command}</strong> <span>{prompt.title}</span>
              <button aria-label="Remove shortcut" onClick={() => { setPrompt(null); ta.current?.focus() }}><X size={12} /></button>
            </span>
          )}
        </div>
      )}
      {secretWarn && (
        <div className="secret-warn" role="alert">
          <ShieldAlert size={18} className="secret-warn-icon" />
          <div className="secret-warn-text">
            <strong>This message looks like it contains a secret</strong>
            <span>{describeFindings(secretWarn.findings)}. Messages are stored with the chat and sent to the model;
              redacting replaces the values with placeholders.</span>
          </div>
          <div className="secret-warn-actions">
            <button className="btn primary sm" autoFocus onClick={() => submit(redactSecrets(secretWarn.content, secretWarn.findings), true)}>Redact &amp; send</button>
            <button className="btn sm" onClick={() => submit(secretWarn.content, true)}>Send anyway</button>
            <button className="btn ghost sm" onClick={() => { setSecretWarn(null); ta.current?.focus() }}>Edit</button>
          </div>
        </div>
      )}
      {files.length > 0 && (
        <div className="composer-files">
          {files.map((f) => (
            <AttachmentChip key={f.key} a={f.att ?? { filename: f.file.name }} uploading={!f.att}
              onRemove={() => setFiles((x) => x.filter((y) => y.key !== f.key))} />
          ))}
        </div>
      )}
      <textarea
        ref={ta}
        value={text}
        autoFocus={autoFocus}
        placeholder={mic === 'recording' ? 'Listening…'
          : prompt ? `Paste the code, error or text for /${prompt.command}…`
            : skill ? `What should Qwen do with the ${skill.title} skill?`
              : placeholder ?? 'How can I help you today? Type / for shortcuts and skills'}
        rows={1}
        onChange={(e) => onTextChange(e.target.value)}
        onPaste={(e) => {
          const pasted = Array.from(e.clipboardData.files)
          if (pasted.length) { e.preventDefault(); addFiles(pasted) }
        }}
        onKeyDown={(e) => {
          if (e.nativeEvent.isComposing) return
          if (slashOpen) {
            const n = slashItems.length
            if (e.key === 'ArrowDown' && n) { e.preventDefault(); setSlashIndex((slashActive + 1) % n); return }
            if (e.key === 'ArrowUp' && n) { e.preventDefault(); setSlashIndex((slashActive - 1 + n) % n); return }
            if ((e.key === 'Enter' || e.key === 'Tab') && n) { e.preventDefault(); pick(slashItems[slashActive]); return }
            if (e.key === 'Escape') { e.preventDefault(); setSlashDismissed(true); return }
          }
          if (e.key === 'Backspace' && (prompt || skill) && !text && e.currentTarget.selectionStart === 0) {
            if (prompt) setPrompt(null)
            else setSkill(null)
            return
          }
          if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() }
        }}
      />
      <div className="composer-bar">
        <div className="composer-left">
          <input ref={fileInput} type="file" multiple hidden onChange={(e) => { addFiles(e.target.files ?? []); e.target.value = '' }} />
          <button className="icon-btn" title="Attach files (images, PDFs, text, code)" onClick={() => fileInput.current?.click()}>
            <Paperclip size={18} />
          </button>
          {model?.thinking && (
            <button className={`toggle${thinking ? ' on' : ''}`} title="Extended thinking"
              onClick={() => void saveSettings({ thinking: !settings.thinking })}>
              <Brain size={15} /> <span>Think</span>
            </button>
          )}
          {model?.tools && (
            <button className={`toggle${webSearch ? ' on' : ''}`}
              title={`Web search (${config?.features.web_search})`}
              onClick={() => void saveSettings({ web_search: !settings.web_search })}>
              <Globe size={15} /> <span>Search</span>
            </button>
          )}
          {analysisAvailable && (
            <button className={`toggle${analysis ? ' on' : ''}`} title="Data analysis: run Python in a sandbox for calculations and charts"
              onClick={() => void saveSettings({ analysis: settings.analysis === false })}>
              <BarChart3 size={15} /> <span>Analyze</span>
            </button>
          )}
          {model?.tools && <ConnectorsMenu />}
        </div>
        <div className="composer-right">
          <ModelPicker value={modelId} onChange={onModelChange} />
          {config?.features.asr && Recorder.supported() && (
            <button className={`icon-btn mic ${mic}`} title={mic === 'recording' ? 'Stop & transcribe' : 'Dictate'}
              onClick={() => void toggleMic()} style={{ ['--lvl' as string]: level }}>
              {mic === 'transcribing' ? <Loader2 size={18} className="spin" /> : <Mic size={18} />}
            </button>
          )}
          {streaming ? (
            <button className="send stop" title="Stop" onClick={onStop}><Square size={14} fill="currentColor" /></button>
          ) : (
            <button className="send" title="Send" disabled={!canSend} onClick={() => submit()}><ArrowUp size={18} /></button>
          )}
        </div>
      </div>
    </div>
  )
}
