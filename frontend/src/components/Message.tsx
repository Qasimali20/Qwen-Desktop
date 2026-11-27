import { LogoMark } from './LogoMark'
import { WorkingLine } from './WorkingLine'
import { useWorkingWord } from '../lib/useWorkingWord'
import { memo, useState, type ReactNode } from 'react'
import { AlertTriangle, BookOpen, Brain, ChevronRight, FileCode2, FileText, FolderTree, Globe, GraduationCap, Image as ImageIcon, Link2, Loader2, FilePen, FilePlus2, Pencil, Plug, RefreshCw, SearchCode, Square, SquareTerminal, Terminal, Volume2, Wrench, Zap } from 'lucide-react'
import { api } from '../api/client'
import type { Attachment, Block, Message, OutputFile } from '../api/types'
import { formatBytes } from '../lib/text'
import { useApp } from '../store/app'
import { useSpeech } from '../store/speech'
import { CopyButton, Markdown } from './Markdown'
import { DiffView } from './DiffView'

// ---------------------------------------------------------------- attachments
export function AttachmentChip({ a, onRemove, uploading }: { a: Attachment | { filename: string; kind?: string; id?: string }; onRemove?: () => void; uploading?: boolean }) {
  const att = a as Attachment
  const isImage = att.kind === 'image' && att.id
  return (
    <div className={`chip${isImage ? ' chip-image' : ''}`} title={att.filename}>
      {uploading ? <Loader2 size={16} className="spin" />
        : isImage ? <img src={api.attachmentUrl(att.id)} alt={att.filename} loading="lazy" />
          : <FileText size={16} />}
      {!isImage && (
        <span className="chip-text">
          <span className="chip-name">{att.filename}</span>
          {att.size !== undefined && (
            <span className="chip-sub">
              {att.meta?.pages ? `${att.meta.pages} pages${att.meta.ocr_pages ? ` · ${att.meta.ocr_pages} OCR` : ''}` : formatBytes(att.size)}
              {!!att.meta?.secrets?.length && (
                <span className="chip-warn" title={`Masked before Qwen reads it: ${att.meta.secrets.map((x) => `${x.label} (line ${x.line})`).join(', ')}`}>
                  {' · '}{att.meta.secrets.length} secret{att.meta.secrets.length > 1 ? 's' : ''} masked
                </span>
              )}
            </span>
          )}
        </span>
      )}
      {onRemove && <button className="chip-x" onClick={onRemove} aria-label="Remove">×</button>}
    </div>
  )
}

// ---------------------------------------------------------------- blocks
function ThinkingBlock({ text, live }: { text: string; live: boolean }) {
  const [open, setOpen] = useState(false)
  const word = useWorkingWord(live)
  return (
    <div className={`thinking${open ? ' open' : ''}`}>
      <button className="thinking-head" onClick={() => setOpen(!open)}>
        <Brain size={14} />
        <span key={live ? word : 'done'} className={live ? 'status-word' : undefined}>{live ? `${word}…` : 'Thought process'}</span>
        <ChevronRight size={14} className="chev" />
      </button>
      {open && <div className="thinking-body"><Markdown text={text} /></div>}
      {!open && live && <div className="thinking-peek">{text.slice(-220)}</div>}
    </div>
  )
}

const TOOL_META: Record<string, { icon: typeof Globe; verb: string; running: string }> = {
  web_search: { icon: Globe, verb: 'Searched the web', running: 'Searching the web…' },
  fetch_url: { icon: Link2, verb: 'Read page', running: 'Reading page…' },
  analyze_image: { icon: ImageIcon, verb: 'Looked at image', running: 'Looking at image…' },
  search_knowledge: { icon: BookOpen, verb: 'Searched knowledge base', running: 'Searching knowledge base…' },
  run_python: { icon: Terminal, verb: 'Ran analysis', running: 'Running analysis…' },
  run_shell: { icon: SquareTerminal, verb: 'Ran', running: 'Running…' },
  write_file: { icon: FilePlus2, verb: 'Wrote', running: 'Writing…' },
  edit_file: { icon: FilePen, verb: 'Edited', running: 'Editing…' },
  grep: { icon: SearchCode, verb: 'Searched for', running: 'Searching files…' },
  read_file: { icon: FileCode2, verb: 'Read', running: 'Reading…' },
  list_files: { icon: FolderTree, verb: 'Listed files', running: 'Listing files…' },
  grep_code: { icon: SearchCode, verb: 'Searched code for', running: 'Searching code…' },
  use_skill: { icon: GraduationCap, verb: 'Using skill', running: 'Loading skill…' },
}

/** Tool label: built-ins from TOOL_META; MCP tools (mcp__<server>__<tool>) by their connector. */
function toolMeta(b: Extract<Block, { type: 'tool' }>): { icon: typeof Globe; verb: string; running: string; arg?: string } {
  if (b.name === 'use_skill' && b.skill) return { ...TOOL_META.use_skill, arg: b.skill.title }
  if (b.name.startsWith('mcp__')) {
    const [, server, tool] = b.name.split('__')
    const label = b.connector?.server ?? server
    const what = b.connector?.tool ?? tool
    return { icon: Plug, verb: `${label}: ${what}`, running: `Using ${label}…`, arg: '' }
  }
  return TOOL_META[b.name] ?? { icon: Wrench, verb: b.name, running: `${b.name}…` }
}

function toolSummary(args: string): string {
  try {
    const a = JSON.parse(args || '{}')
    if (a.path && (a.start_line || a.end_line)) return `${a.path}:${a.start_line ?? 1}${a.end_line ? `-${a.end_line}` : ''}`
    if (typeof a.command === 'string') return firstLine(a.command)
    return a.query ?? a.url ?? a.question ?? a.pattern ?? a.path ?? ''
  } catch {
    return ''
  }
}

function firstLine(text: string): string {
  const lines = text.trim().split('\n')
  const head = lines[0].length > 90 ? `${lines[0].slice(0, 90)}…` : lines[0]
  return lines.length > 1 ? `${head} (+${lines.length - 1} lines)` : head
}

/** Tools whose input is a program: shown as highlighted source above the output. */
const SOURCE_ARG: Record<string, { field: string; lang: string }> = {
  run_python: { field: 'code', lang: 'python' },
  run_shell: { field: 'command', lang: 'bash' },
}

function sourceOf(args: string, field: string): string {
  try { return JSON.parse(args || '{}')[field] ?? '' } catch { return '' }
}

function Outputs({ files }: { files: OutputFile[] }) {
  const images = files.filter((f) => f.kind === 'image')
  const others = files.filter((f) => f.kind !== 'image')
  return (
    <div className="outputs">
      {images.map((f) => (
        <a key={f.id} className="output-figure" href={api.attachmentUrl(f.id)} target="_blank" rel="noreferrer" title={f.filename}>
          <img src={api.attachmentUrl(f.id)} alt={f.filename} loading="lazy" />
        </a>
      ))}
      {!!others.length && (
        <div className="output-files">
          {others.map((f) => (
            <a key={f.id} className="chip" href={`${api.attachmentUrl(f.id)}?download=1`} title={`Download ${f.filename}`}>
              <FileText size={16} />
              <span className="chip-text"><span className="chip-name">{f.filename}</span><span className="chip-sub">{formatBytes(f.size)} · download</span></span>
            </a>
          ))}
        </div>
      )}
    </div>
  )
}

function ToolBlock({ b }: { b: Extract<Block, { type: 'tool' }> }) {
  const [open, setOpen] = useState(false)
  const meta = toolMeta(b)
  const Icon = meta.icon
  const running = b.result === null
  let results: { title: string; url: string }[] | null = null
  if (b.name === 'web_search' && b.result && b.ok) {
    try { results = JSON.parse(b.result.slice(b.result.indexOf('['))) } catch { /* plain text */ }
  }
  return (
    <div className={`tool${b.ok === false ? ' failed' : ''}`}>
      <button className="tool-head" onClick={() => setOpen(!open)}>
        {running ? <Loader2 size={14} className="spin" /> : b.ok === false ? <AlertTriangle size={14} /> : <Icon size={14} />}
        <span className="tool-verb">{running ? meta.running : meta.verb}</span>
        <span className="tool-arg">{meta.arg ?? toolSummary(b.arguments)}</span>
        {results && <span className="tool-count">{results.length} results</span>}
        {b.sources && <span className="tool-count">{b.sources.length} passages</span>}
        {b.file && <span className="tool-count diffstat"><span className="add">+{b.file.added}</span> <span className="del">−{b.file.removed}</span></span>}
        <ChevronRight size={14} className={`chev${open ? ' rot' : ''}`} />
      </button>
      {open && (
        <div className="tool-body">
          {b.file ? (
            <div className="tool-diff"><DiffView diff={b.file.diff} /></div>
          ) : SOURCE_ARG[b.name] ? (
            <div className="analysis">
              <Markdown text={`\`\`\`${SOURCE_ARG[b.name].lang}\n${sourceOf(b.arguments, SOURCE_ARG[b.name].field)}\n\`\`\``} />
              {b.result !== null && <pre className="analysis-out">{b.result}</pre>}
            </div>
          ) : b.sources ? (
            <ul className="tool-results sources">
              {b.sources.map((src) => (
                <li key={src.n}>
                  {src.kind === 'code' && src.repo_id ? (
                    <button className="src-link" onClick={() => useApp.getState().openCode({
                      repoId: src.repo_id!, path: src.filename.slice(src.filename.indexOf('/') + 1),
                      start: src.start_line, end: src.end_line,
                    })}>
                      <span className="src-n">{src.n}</span>
                      <span className="src-body">
                        <span className="src-title">{src.filename}:{src.start_line}–{src.end_line}</span>
                        <span className="src-snippet code">{src.snippet}</span>
                      </span>
                    </button>
                  ) : (
                    <a href={`${api.attachmentUrl(src.attachment_id ?? '')}${src.page ? `#page=${src.page}` : ''}`} target="_blank" rel="noreferrer">
                      <span className="src-n">{src.n}</span>
                      <span className="src-body">
                        <span className="src-title">{src.filename}{src.page ? ` · p. ${src.page}` : ''}</span>
                        <span className="src-snippet">{src.snippet}</span>
                      </span>
                    </a>
                  )}
                </li>
              ))}
            </ul>
          ) : results ? (
            <ul className="tool-results">
              {results.map((r) => (
                <li key={r.url}><a href={r.url} target="_blank" rel="noreferrer">{r.title}</a><span>{new URL(r.url).hostname}</span></li>
              ))}
            </ul>
          ) : (
            <pre>{b.result ?? 'Running…'}</pre>
          )}
        </div>
      )}
      {!!b.outputs?.length && <Outputs files={b.outputs} />}
    </div>
  )
}

// ---------------------------------------------------------------- step grouping
const GROUP_MIN = 3

type Segment = { kind: 'text'; start: number } | { kind: 'steps'; start: number; items: number[] }

/** Split blocks into text and runs of consecutive thinking/tool steps. */
function segments(blocks: Block[]): Segment[] {
  const out: Segment[] = []
  blocks.forEach((b, i) => {
    if (b.type === 'text' || (b.type === 'tool' && b.outputs?.length)) return void out.push({ kind: 'text', start: i })
    const last = out.at(-1)
    if (last?.kind === 'steps') last.items.push(i)
    else out.push({ kind: 'steps', start: i, items: [i] })
  })
  return out
}

function StepGroup({ blocks, children }: { blocks: Block[]; children: ReactNode }) {
  const [open, setOpen] = useState(false)
  const tools = blocks.filter((b) => b.type === 'tool').length
  const thoughts = blocks.length - tools
  const label = [tools && `${tools} tool call${tools > 1 ? 's' : ''}`, thoughts && `${thoughts} thought${thoughts > 1 ? 's' : ''}`]
    .filter(Boolean).join(' · ')
  return (
    <div className={`step-group${open ? ' open' : ''}`}>
      <button className="tool-head" onClick={() => setOpen(!open)}>
        <Wrench size={14} /><span className="tool-verb">Worked through {blocks.length} steps</span>
        <span className="tool-arg">{label}</span>
        <ChevronRight size={14} className={`chev${open ? ' rot' : ''}`} />
      </button>
      {open && <div className="step-body">{children}</div>}
    </div>
  )
}

// ---------------------------------------------------------------- messages
function UserMessageImpl({ m, onEdit, disabled }: { m: Message; onEdit: (m: Message, text: string) => void; disabled: boolean }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(m.content)
  return (
    <div className="msg msg-user">
      {!!m.attachments?.length && (
        <div className="msg-attachments">{m.attachments.map((a) => <AttachmentChip key={a.id} a={a} />)}</div>
      )}
      {m.meta?.skill && (
        <span className="prompt-chip skill sent" title={`Skill: ${m.meta.skill.title}`}><GraduationCap size={12} /> <strong>{m.meta.skill.title}</strong> <span>skill</span></span>
      )}
      {m.meta?.prompt && (
        <span className="prompt-chip sent" title={m.meta.prompt.title}><Zap size={12} /> <strong>/{m.meta.prompt.command}</strong> <span>{m.meta.prompt.title}</span></span>
      )}
      {editing ? (
        <div className="edit-box">
          <textarea autoFocus value={draft} onChange={(e) => setDraft(e.target.value)} rows={Math.min(12, draft.split('\n').length + 1)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); setEditing(false); onEdit(m, draft) }
              if (e.key === 'Escape') setEditing(false)
            }} />
          <div className="edit-actions">
            <button className="btn ghost" onClick={() => { setEditing(false); setDraft(m.content) }}>Cancel</button>
            <button className="btn primary" disabled={!draft.trim() && !m.meta?.prompt && !m.meta?.skill} onClick={() => { setEditing(false); onEdit(m, draft) }}>Save & submit</button>
          </div>
        </div>
      ) : (
        m.content && <div className="bubble">{m.content}</div>
      )}
      {!editing && !m.id.startsWith('tmp-') && (
        <div className="msg-actions">
          <CopyButton text={m.content} />
          {!disabled && <button className="icon-btn sm" title="Edit" onClick={() => { setDraft(m.content); setEditing(true) }}><Pencil size={14} /></button>}
        </div>
      )}
    </div>
  )
}

export const UserMessage = memo(UserMessageImpl)

/** Nothing is streaming into view: before the first token, or after a tool finished while the model
 *  works on its next step. */
function waitingForModel(blocks: Block[]): boolean {
  const last = blocks[blocks.length - 1]
  return !last || (last.type === 'tool' && last.result !== null)
}

function AssistantMessageImpl({ m, live, status, isLast, onRegenerate }: {
  m: Message; live: boolean; status: string | null; isLast: boolean; onRegenerate: () => void
}) {
  const models = useApp((s) => s.config?.models)
  const { speakingId, loading, speak, stop } = useSpeech()
  const speaking = speakingId === m.id
  const text = m.blocks.filter((b) => b.type === 'text').map((b) => (b as { text: string }).text).join('\n\n') || m.content
  const lastIdx = m.blocks.length - 1
  const model = models?.find((x) => x.id === m.model_id)

  return (
    <div className="msg msg-assistant">
      <div className="avatar" aria-hidden><LogoMark /></div>
      <div className="msg-body">
        {segments(m.blocks).map((seg) => {
          if (seg.kind === 'text') {
            const i = seg.start
            const blk = m.blocks[i]
            if (blk.type === 'tool') return <ToolBlock key={i} b={blk} />
            const b = blk as { text: string }
            return <Markdown key={i} text={b.text} artifactKey={`${m.id}:${i}`} streaming={live && i === lastIdx} />
          }
          const steps = seg.items.map((i) => {
            const b = m.blocks[i]
            return b.type === 'thinking'
              ? <ThinkingBlock key={i} text={b.text} live={live && i === lastIdx} />
              : <ToolBlock key={i} b={b as Extract<Block, { type: 'tool' }>} />
          })
          const running = live && seg.items.includes(lastIdx)
          return seg.items.length >= GROUP_MIN && !running
            ? <StepGroup key={seg.start} blocks={seg.items.map((i) => m.blocks[i])}>{steps}</StepGroup>
            : steps
        })}
        {!m.blocks.length && m.content && <Markdown text={m.content} artifactKey={`${m.id}:0`} />}
        {live && (status || waitingForModel(m.blocks)) && <WorkingLine label={status} />}
        {m.usage?.error && m.usage.error !== 'stopped' && <div className="msg-error"><AlertTriangle size={14} /> {m.usage.error}</div>}
        {!live && (
          <div className="msg-actions">
            <CopyButton text={text} />
            <button className="icon-btn sm" title={speaking ? 'Stop' : 'Read aloud'} onClick={() => (speaking ? stop() : speak(m.id, text))}>
              {speaking ? (loading ? <Loader2 size={14} className="spin" /> : <Square size={13} />) : <Volume2 size={14} />}
            </button>
            {isLast && <button className="icon-btn sm" title="Retry" onClick={onRegenerate}><RefreshCw size={14} /></button>}
            <span className="msg-meta">
              {model?.label ?? m.model_id}
              {m.usage?.completion_tokens ? ` · ${m.usage.completion_tokens} tok` : ''}
              {m.usage?.duration_s ? ` · ${m.usage.duration_s}s` : ''}
              {m.usage?.error === 'stopped' ? ' · stopped' : ''}
            </span>
          </div>
        )}
      </div>
    </div>
  )
}

export const AssistantMessage = memo(AssistantMessageImpl)
