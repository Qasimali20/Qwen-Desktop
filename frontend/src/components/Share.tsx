import { createPortal } from 'react-dom'
import { useCallback, useEffect, useState } from 'react'
import { Check, Copy, Eye, Link2, Loader2, MessageSquare, RefreshCw, Trash2, X } from 'lucide-react'
import { api } from '../api/client'
import type { Message, ShareLink, SharedChat } from '../api/types'
import { useApp } from '../store/app'
import { useChat } from '../store/chat'
import { AssistantMessage, UserMessage } from './Message'

const linkUrl = (token: string) => `${location.origin}/share/${token}`

function when(ts: number): string {
  return new Date(ts * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
}

/** "Share" in the chat header: create, copy, refresh or delete the conversation's read-only link. */
export function ShareDialog({ conversationId, onClose }: { conversationId: string; onClose: () => void }) {
  const notify = useApp((s) => s.notify)
  const messageCount = useChat((s) => s.messages.length)
  const [share, setShare] = useState<ShareLink | null | undefined>(undefined)
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let live = true
    api.shareOf(conversationId).then((s) => live && setShare(s)).catch((e) => { notify((e as Error).message); onClose() })
    return () => { live = false }
  }, [conversationId, notify, onClose])

  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])

  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    try { await fn() } catch (e) { notify((e as Error).message) } finally { setBusy(false) }
  }

  const copy = async (token: string) => {
    try {
      await navigator.clipboard.writeText(linkUrl(token))
    } catch {
      // Clipboard API needs a secure context; fall back to selecting the field so Ctrl+C works.
      document.querySelector<HTMLInputElement>('.share-url input')?.select()
      return
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 1600)
  }

  const create = () => run(async () => {
    const s = await api.saveShare(conversationId)
    setShare(s)
    void copy(s.id)
  })
  const refresh = () => run(async () => { setShare(await api.saveShare(conversationId)) })
  const remove = () => run(async () => { await api.deleteShare(conversationId); setShare(null) })

  const stale = !!share && share.message_count !== messageCount

  // Portalled: an ancestor in the chat header is transformed, which would trap a fixed overlay inside it.
  return createPortal(
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal share-dialog" role="dialog" aria-label="Share chat">
        <header><h2>Share chat</h2><button className="icon-btn" aria-label="Close" onClick={onClose}><X size={18} /></button></header>
        {share === undefined ? (
          <div className="muted"><Loader2 size={16} className="spin" /> Loading…</div>
        ) : share === null ? (
          <>
            <p className="share-lead">
              Create a read-only link to this chat. Anyone signed in to Qwen Desktop who has the link can view it,
              including any files and charts in it. Messages you send later are not included until you update the link.
            </p>
            <div className="share-actions">
              <button className="btn primary" disabled={busy} onClick={() => void create()}>
                {busy ? <Loader2 size={15} className="spin" /> : <Link2 size={15} />} Create link
              </button>
            </div>
          </>
        ) : (
          <>
            <div className="share-url">
              <input readOnly value={linkUrl(share.id)} onFocus={(e) => e.target.select()} aria-label="Share link" />
              <button className="btn primary sm" onClick={() => void copy(share.id)}>
                {copied ? <Check size={14} /> : <Copy size={14} />} {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
            <div className="share-meta muted small">
              <span><MessageSquare size={13} /> {share.message_count} message{share.message_count === 1 ? '' : 's'} shared</span>
              <span><Eye size={13} /> {share.views} view{share.views === 1 ? '' : 's'}</span>
              <span>Updated {when(share.updated_at)}</span>
            </div>
            {stale && <p className="share-stale small">This chat has changed since the link was last updated.</p>}
            <div className="share-actions">
              <button className="btn ghost danger-text" disabled={busy} onClick={() => void remove()}><Trash2 size={14} /> Delete link</button>
              <button className={`btn ${stale ? 'primary' : 'ghost'}`} disabled={busy} onClick={() => void refresh()}>
                {busy ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />} Update to latest
              </button>
            </div>
          </>
        )}
      </div>
    </div>,
    document.body,
  )
}

const noop = () => {}

/** /share/:token — a read-only view of a shared chat. */
export function SharedView({ token }: { token: string }) {
  const navigate = useApp((s) => s.navigate)
  const [chat, setChat] = useState<SharedChat | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    api.sharedChat(token).then(setChat).catch((e) => setError((e as Error).message))
  }, [token])
  useEffect(load, [load])

  if (error) {
    return (
      <div className="chat shared">
        <div className="shared-empty">
          <Link2 size={28} />
          <h2>Link not available</h2>
          <p className="muted">{error}</p>
          <button className="btn ghost" onClick={() => navigate({ name: 'home' })}>Start a new chat</button>
        </div>
      </div>
    )
  }
  if (!chat) return <div className="chat shared"><div className="loading-msgs"><Loader2 size={16} className="spin" /> Loading…</div></div>

  return (
    <div className="chat shared">
      <header className="chat-header shared-header">
        <span className="title-btn static">{chat.title}</span>
      </header>
      <div className="messages">
        <div className="messages-inner">
          <div className="shared-banner">
            <Link2 size={14} />
            <span>Shared by <strong>{chat.owner}</strong> · {when(chat.updated_at)} · read-only</span>
            {chat.is_owner && (
              <button className="btn ghost sm" onClick={() => navigate({ name: 'chat', id: chat.conversation_id })}>Open original</button>
            )}
          </div>
          {chat.messages.map((m: Message) =>
            m.role === 'user'
              ? <UserMessage key={m.id} m={m} onEdit={noop} disabled />
              : <AssistantMessage key={m.id} m={m} live={false} status={null} isLast={false} onRegenerate={noop} />,
          )}
        </div>
      </div>
    </div>
  )
}
