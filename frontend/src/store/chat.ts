import { create } from 'zustand'
import { api, streamMessage, type SendBody } from '../api/client'
import type { Attachment, Block, Conversation, Message, PromptShortcut, Skill, StreamEvent } from '../api/types'
import { useApp } from './app'

interface Streaming {
  conversationId: string
  assistantId: string | null
  status: string | null
}

interface ChatState {
  conversation: Conversation | null
  messages: Message[]
  loading: boolean
  streaming: Streaming | null
  error: string | null

  load: (id: string, force?: boolean) => Promise<void>
  reset: () => void
  send: (opts: {
    content: string
    attachments: Attachment[]
    modelId?: string
    projectId?: string | null
    workspaceId?: string | null
    thinking: boolean
    webSearch: boolean
    analysis: boolean
    prompt?: PromptShortcut | null
    skill?: Skill | null
    editMessageId?: string
  }) => Promise<void>
  regenerate: (opts: { modelId?: string; thinking: boolean; webSearch: boolean; analysis: boolean }) => Promise<void>
  stop: () => void
  setModel: (modelId: string) => void
}

let controller: AbortController | null = null

/** MCP connectors the user switched on that are still available. */
function activeConnectors(): string[] {
  const { settings, connectors } = useApp.getState()
  const on = new Set(settings.connectors ?? [])
  return connectors.filter((c) => on.has(c.id)).map((c) => c.id)
}

// Deltas arrive far faster than the screen refreshes: buffer them and apply once per frame.
let pending: { index: number; text: string }[] = []
let frame = 0

export const useChat = create<ChatState>((set, get) => {
  const flush = () => {
    frame = 0
    if (!pending.length) return
    const batch = pending
    pending = []
    set((s) => {
      const id = s.streaming?.assistantId
      if (!id) return s
      return {
        messages: s.messages.map((m) => {
          if (m.id !== id) return m
          const blocks = m.blocks.slice()
          for (const d of batch) {
            const b = blocks[d.index]
            if (b && b.type !== 'tool') blocks[d.index] = { ...b, text: b.text + d.text }
          }
          return { ...m, blocks }
        }),
      }
    })
  }

  const patchAssistant = (fn: (m: Message) => Message) => set((s) => {
    const id = s.streaming?.assistantId
    return id ? { messages: s.messages.map((m) => (m.id === id ? fn(m) : m)) } : s
  })

  const handle = (ev: StreamEvent) => {
    if (ev.type !== 'delta' && pending.length) flush()
    switch (ev.type) {
      case 'user_message':
        set((s) => ({ messages: [...s.messages.filter((m) => !m.id.startsWith('tmp-')), ev.message] }))
        break
      case 'assistant_start': {
        const conv = get().conversation
        const placeholder: Message = {
          id: ev.id, conversation_id: conv?.id ?? '', seq: Number.MAX_SAFE_INTEGER, role: 'assistant', content: '',
          blocks: [], attachment_ids: [], model_id: ev.model_id, usage: null, created_at: Date.now() / 1000,
        }
        set((s) => ({ messages: [...s.messages, placeholder], streaming: s.streaming && { ...s.streaming, assistantId: ev.id } }))
        break
      }
      case 'block_start':
        patchAssistant((m) => {
          const blocks = m.blocks.slice()
          blocks[ev.index] = { ...ev.block } as Block
          return { ...m, blocks }
        })
        set((s) => ({ streaming: s.streaming && { ...s.streaming, status: null } }))
        break
      case 'delta':
        pending.push({ index: ev.index, text: ev.text })
        if (!frame) frame = requestAnimationFrame(flush)
        break
      case 'tool_result':
        patchAssistant((m) => {
          const blocks = m.blocks.slice()
          const b = blocks[ev.index]
          if (b?.type === 'tool') {
            blocks[ev.index] = { ...b, result: ev.result, ok: ev.ok, sources: ev.sources ?? b.sources, outputs: ev.outputs ?? b.outputs }
          }
          return { ...m, blocks }
        })
        break
      case 'status':
        set((s) => ({ streaming: s.streaming && { ...s.streaming, status: ev.text } }))
        break
      case 'title': {
        const conv = get().conversation
        if (conv) {
          set({ conversation: { ...conv, title: ev.title } })
          useApp.getState().upsertConversation({ ...conv, title: ev.title })
        }
        break
      }
      case 'done':
        set((s) => ({ messages: s.messages.map((m) => (m.id === ev.message.id ? ev.message : m)) }))
        break
      case 'error':
        if (ev.saved) set((s) => ({ messages: s.messages.map((m) => (m.id === ev.saved!.id ? ev.saved! : m)) }))
        set({ error: ev.message })
        break
    }
  }

  const run = async (conversationId: string, body: SendBody) => {
    controller?.abort()
    controller = new AbortController()
    set({ streaming: { conversationId, assistantId: null, status: null }, error: null })
    let completed = false
    try {
      for await (const ev of streamMessage(conversationId, body, controller.signal)) {
        if (ev.type === 'done') completed = true
        if (get().conversation?.id !== conversationId) continue // user navigated away; server still persists
        handle(ev)
      }
    } catch (e) {
      if ((e as Error).name !== 'AbortError') set({ error: (e as Error).message })
    } finally {
      if (frame) cancelAnimationFrame(frame)
      flush()
      set({ streaming: null })
      controller = null
      const conv = get().conversation
      if (conv?.id === conversationId) {
        useApp.getState().upsertConversation({ ...conv, updated_at: Date.now() / 1000 })
        // A stopped or failed stream is persisted server-side: resync once the server has saved it.
        if (!completed) {
          setTimeout(() => get().conversation?.id === conversationId && !get().streaming && void get().load(conversationId, true), 400)
        }
      }
    }
  }

  return {
    conversation: null,
    messages: [],
    loading: false,
    streaming: null,
    error: null,

    load: async (id, force = false) => {
      // Live or already-loaded state is authoritative (single-user app): skip the refetch.
      if (!force && (get().streaming?.conversationId === id || get().conversation?.id === id)) return
      set({ loading: get().conversation?.id !== id, error: null })
      try {
        const conv = await api.conversation(id)
        set({ conversation: conv, messages: conv.messages, loading: false })
      } catch (e) {
        set({ loading: false, conversation: null, messages: [] })
        useApp.getState().notify((e as Error).message)
        useApp.getState().navigate({ name: 'home' }, true)
      }
    },

    reset: () => set({ conversation: null, messages: [], error: null }),

    send: async ({ content, attachments, modelId, projectId, workspaceId, thinking, webSearch, analysis, prompt, skill, editMessageId }) => {
      const app = useApp.getState()
      let conv = get().conversation
      if (!conv) {
        const created = await api.createConversation({ model_id: modelId, project_id: projectId ?? null,
          workspace_id: (projectId && workspaceId) || null })
        const project = app.projects.find((p) => p.id === projectId) ?? null
        conv = { ...created, system_prompt: '', messages: [], project }
        // Claim the stream before navigating so the route change doesn't trigger a reload.
        set({ conversation: conv, messages: [], streaming: { conversationId: conv.id, assistantId: null, status: null } })
        app.upsertConversation(created)
        app.navigate({ name: 'chat', id: conv.id }, true)
      }
      // Optimistic user bubble; replaced by the persisted one from the stream.
      const optimistic: Message = {
        id: `tmp-${Date.now()}`, conversation_id: conv.id, seq: 0, role: 'user', content, blocks: [],
        attachment_ids: attachments.map((a) => a.id), attachments, model_id: null, usage: null,
        meta: {
          ...(prompt ? { prompt: { id: prompt.id, command: prompt.command, title: prompt.title } } : {}),
          ...(skill ? { skill: { id: skill.id, name: skill.name, title: skill.title } } : {}),
        },
        created_at: Date.now() / 1000,
      }
      set((s) => {
        let msgs = s.messages
        if (editMessageId) {
          const at = msgs.findIndex((m) => m.id === editMessageId)
          if (at >= 0) msgs = msgs.slice(0, at)
        }
        return { messages: [...msgs, optimistic] }
      })
      await run(conv.id, {
        content, attachment_ids: attachments.map((a) => a.id), model_id: modelId, thinking,
        web_search: webSearch, analysis, prompt_id: prompt?.id, skill_id: skill?.id, edit_message_id: editMessageId,
        connectors: activeConnectors(),
      })
    },

    regenerate: async ({ modelId, thinking, webSearch, analysis }) => {
      const conv = get().conversation
      if (!conv) return
      set((s) => {
        const lastUser = s.messages.map((m) => m.role).lastIndexOf('user')
        return { messages: s.messages.slice(0, lastUser + 1) }
      })
      await run(conv.id, { regenerate: true, model_id: modelId, thinking, web_search: webSearch, analysis, connectors: activeConnectors() })
    },

    stop: () => controller?.abort(),

    setModel: (modelId) => {
      const conv = get().conversation
      if (!conv) return
      set({ conversation: { ...conv, model_id: modelId } })
      void api.patchConversation(conv.id, { model_id: modelId })
    },
  }
})
