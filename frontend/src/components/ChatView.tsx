import { LogoMark } from './LogoMark'
import { SparkMark } from './SparkMark'
import { WorkingLine } from './WorkingLine'
import { greeting } from '../lib/phrases'
import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowDown, FolderOpen, Share2 } from 'lucide-react'
import { api } from '../api/client'
import type { Message } from '../api/types'
import { useApp } from '../store/app'
import { useChat } from '../store/chat'
import { useSpeech } from '../store/speech'
import { Composer, type SendPayload } from './Composer'
import { AssistantMessage, UserMessage } from './Message'
import { ShareDialog } from './Share'
import { SummaryDivider } from './SummaryDivider'
import { WorkspaceChip } from './ChangesPanel'
import { WorkspacePicker } from './WorkspaceSection'

/** Re-renders every minute so the greeting follows the time of day while the page stays open. */
function useMinute(): Date {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 60_000)
    return () => clearInterval(t)
  }, [])
  return now
}

function Greeting() {
  const name = useApp((s) => s.user?.display_name)
  const now = useMinute()
  const [seed] = useState(() => Math.floor(Math.random() * 1000))
  return <h1 className="greeting"><SparkMark className="greeting-mark" size={40} /> {greeting(name, now, seed)}</h1>
}

export function ChatView({ conversationId, projectId }: { conversationId?: string; projectId?: string }) {
  const { conversation, messages, loading, streaming, error, load, reset, send, regenerate, stop, setModel } = useChat()
  const { defaultModelId, saveSettings, settings, projects, navigate, config } = useApp()
  const [draftModel, setDraftModel] = useState<string | undefined>()
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const [showJump, setShowJump] = useState(false)

  useEffect(() => {
    if (conversationId) void load(conversationId)
    else reset()
  }, [conversationId, load, reset])

  // A new chat that will work in a workspace starts on the workspace model (Qwen Coder) when it is online.
  const draftProject = conversationId ? null : projectId ?? null
  const inWorkspace = useApp((s) => !!draftProject && !!s.workspaceChoice[draftProject])
  const workspaceModel = inWorkspace
    ? config?.models.find((m) => m.id === config.workspace_model && m.online !== false)?.id
    : undefined
  const modelId = conversation?.model_id ?? draftModel ?? workspaceModel ?? defaultModelId()
  const onModelChange = (id: string) => {
    if (conversation) setModel(id)
    else setDraftModel(id)
    // A pick made for a workspace chat should not become the default for ordinary chats.
    if (!inWorkspace && !conversation?.workspace) void saveSettings({ model_id: id })
  }

  // Keep the view pinned to the bottom while streaming unless the user scrolled up.
  useLayoutEffect(() => {
    const el = scroller.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  }, [messages, streaming?.status])

  useEffect(() => { stick.current = true }, [conversationId])

  const onScroll = () => {
    const el = scroller.current!
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    stick.current = atBottom
    setShowJump(!atBottom)
  }

  // Voice mode: speak each finished reply.
  const wasStreaming = useRef(false)
  useEffect(() => {
    if (wasStreaming.current && !streaming && settings.auto_speak) {
      const last = useChat.getState().messages.at(-1)
      if (last?.role === 'assistant' && last.content) void useSpeech.getState().speak(last.id, last.content)
    }
    wasStreaming.current = !!streaming
  }, [streaming, settings.auto_speak])

  const isStreaming = !!streaming && streaming.conversationId === conversation?.id
  const opts = { thinking: !!settings.thinking, webSearch: !!settings.web_search, analysis: settings.analysis !== false }

  const onSend = useCallback((p: SendPayload) => {
    stick.current = true
    useSpeech.getState().stop()
    const pid = conversation?.project_id ?? projectId ?? null
    const workspaceId = pid ? useApp.getState().workspaceChoice[pid] ?? null : null
    void send({ ...p, modelId, projectId: pid, workspaceId })
  }, [send, modelId, conversation?.project_id, projectId])

  const onEdit = useCallback((m: Message, text: string) => {
    void send({ content: text, attachments: m.attachments ?? [], modelId, thinking: opts.thinking,
      webSearch: opts.webSearch, analysis: opts.analysis, editMessageId: m.id })
  }, [send, modelId, opts.thinking, opts.webSearch, opts.analysis])

  const onRegenerate = useCallback(() => void regenerate({ modelId, ...opts }), [regenerate, modelId, opts.thinking, opts.webSearch, opts.analysis]) // eslint-disable-line react-hooks/exhaustive-deps

  const project = conversation?.project ?? projects.find((p) => p.id === projectId)
  const empty = !conversationId && !messages.length

  if (empty) {
    return (
      <div className="chat home">
        <div className="home-inner">
          {project && (
            <button className="project-badge" onClick={() => navigate({ name: 'project', id: project.id })}>
              <FolderOpen size={14} /> {project.name}
            </button>
          )}
          <Greeting />
          {project && <WorkspacePicker projectId={project.id} />}
          <Composer modelId={modelId} onModelChange={onModelChange} onSend={onSend} onStop={stop} streaming={false} autoFocus />
        </div>
      </div>
    )
  }

  const lastAssistant = messages.map((m) => m.role).lastIndexOf('assistant')
  // Shown only while the message it ends at exists: an edit or retry above it makes the summary stale.
  const summary = conversation?.context_summary
  const summaryAfter = summary && messages.some((m) => m.id === summary.upto_id) ? summary.upto_id : null
  return (
    <div className="chat">
      <ChatHeader />
      <div className="messages" ref={scroller} onScroll={onScroll}>
        <div className="messages-inner">
          {loading ? <div className="loading-msgs"><span className="pulse" /> Loading…</div> : messages.map((m, i) => (
            <Fragment key={m.id}>
              {m.role === 'user'
                ? <UserMessage m={m} onEdit={onEdit} disabled={isStreaming} />
                : <AssistantMessage m={m} live={isStreaming && m.id === streaming?.assistantId}
                  status={isStreaming ? streaming!.status : null} isLast={i === lastAssistant && !isStreaming}
                  onRegenerate={onRegenerate} />}
              {m.id === summaryAfter && summary && <SummaryDivider summary={summary} />}
            </Fragment>
          ))}
          {isStreaming && !streaming?.assistantId && (
            <div className="msg msg-assistant"><div className="avatar" aria-hidden><LogoMark /></div><div className="msg-body"><WorkingLine /></div></div>
          )}
          {error && !isStreaming && (
            <div className="chat-error">
              {error}
              <button className="btn ghost" onClick={onRegenerate}>Retry</button>
            </div>
          )}
        </div>
      </div>
      {showJump && (
        <button className="jump" onClick={() => { stick.current = true; scroller.current!.scrollTo({ top: scroller.current!.scrollHeight, behavior: 'smooth' }) }}>
          <ArrowDown size={16} />
        </button>
      )}
      <div className="composer-dock">
        <Composer modelId={modelId} onModelChange={onModelChange} onSend={onSend} onStop={stop} streaming={isStreaming}
          placeholder="Reply to Qwen…" />
        <div className="disclaimer">Qwen runs on your own Spark cluster. It can make mistakes — verify important information.</div>
      </div>
    </div>
  )
}

function ChatHeader() {
  const conversation = useChat((s) => s.conversation)
  const { upsertConversation, navigate } = useApp()
  const hasMessages = useChat((s) => s.messages.length > 0 && !s.streaming)
  const [editing, setEditing] = useState(false)
  const [title, setTitle] = useState('')
  const [sharing, setSharing] = useState(false)
  const closeShare = useCallback(() => setSharing(false), [])
  if (!conversation) return <header className="chat-header" />

  const save = async () => {
    setEditing(false)
    const t = title.trim()
    if (!t || t === conversation.title) return
    useChat.setState({ conversation: { ...conversation, title: t } })
    upsertConversation(await api.patchConversation(conversation.id, { title: t }))
  }

  return (
    <header className="chat-header">
      {conversation.project && (
        <button className="crumb" onClick={() => navigate({ name: 'project', id: conversation.project!.id })}>
          <FolderOpen size={14} /> {conversation.project.name} <span className="sep">/</span>
        </button>
      )}
      {editing ? (
        <input className="title-input" autoFocus value={title} onChange={(e) => setTitle(e.target.value)}
          onBlur={() => void save()} onKeyDown={(e) => { if (e.key === 'Enter') void save(); if (e.key === 'Escape') setEditing(false) }} />
      ) : (
        <button className="title-btn" title="Rename" onClick={() => { setTitle(conversation.title); setEditing(true) }}>
          {conversation.title}
        </button>
      )}
      <div className="chat-header-actions">
        {conversation.workspace && <WorkspaceChip workspaceId={conversation.workspace.id} name={conversation.workspace.name} />}
        <button className="btn ghost sm" disabled={!hasMessages} title="Create a read-only link to this chat" onClick={() => setSharing(true)}>
          <Share2 size={14} /> Share
        </button>
      </div>
      {sharing && <ShareDialog conversationId={conversation.id} onClose={closeShare} />}
    </header>
  )
}
