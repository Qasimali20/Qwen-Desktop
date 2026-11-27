import { create } from 'zustand'
import { api, ApiError, setUnauthorizedHandler } from '../api/client'
import type { AppConfig, Connector, ConversationSummary, Project, PromptShortcut, Skill, User, UserSettings } from '../api/types'

export type Route =
  | { name: 'home'; projectId?: string }
  | { name: 'chat'; id: string }
  | { name: 'project'; id: string }
  | { name: 'projects' }
  | { name: 'admin' }
  | { name: 'shortcuts' }
  | { name: 'skills' }
  | { name: 'developer' }
  | { name: 'share'; token: string }

export interface Artifact {
  key: string // message id + block position, stable across re-renders
  title: string
  lang: string
  code: string
}

function parseRoute(path: string): Route {
  const [, a, b] = path.split('/')
  if (a === 'chat' && b) return { name: 'chat', id: b }
  if (a === 'project' && b) return { name: 'project', id: b }
  if (a === 'projects') return { name: 'projects' }
  if (a === 'admin') return { name: 'admin' }
  if (a === 'shortcuts') return { name: 'shortcuts' }
  if (a === 'skills') return { name: 'skills' }
  if (a === 'developer') return { name: 'developer' }
  if (a === 'share' && b) return { name: 'share', token: b }
  return { name: 'home' }
}

function routePath(r: Route): string {
  switch (r.name) {
    case 'chat': return `/chat/${r.id}`
    case 'project': return `/project/${r.id}`
    case 'projects': return '/projects'
    case 'admin': return '/admin'
    case 'shortcuts': return '/shortcuts'
    case 'skills': return '/skills'
    case 'developer': return '/developer'
    case 'share': return `/share/${r.token}`
    default: return '/'
  }
}

export interface CodeView { repoId: string; path: string; start?: number; end?: number }

/** The workspace a new chat in a project will work in (per project; null = plain chat). */
type WorkspaceChoice = Record<string, string | null>

interface AppState {
  /** undefined = not checked yet, null = signed out */
  user: User | null | undefined
  config: AppConfig | null
  settings: UserSettings
  conversations: ConversationSummary[]
  projects: Project[]
  /** Prompt shortcuts visible to the user; a personal one shadows a team one with the same command. */
  prompts: PromptShortcut[]
  /** Skills (expert playbooks): built-in, team and the user's own. */
  skills: Skill[]
  /** MCP connectors the user can switch on (Settings → connectors chosen are in settings.connectors). */
  connectors: Connector[]
  route: Route
  sidebarOpen: boolean
  settingsOpen: boolean
  artifact: Artifact | null
  /** Source file opened from a code citation. */
  codeView: CodeView | null
  workspaceChoice: WorkspaceChoice
  chooseWorkspace: (projectId: string, workspaceId: string | null) => void
  toast: string | null

  init: () => Promise<void>
  bootstrap: () => Promise<void>
  signIn: (username: string, password: string) => Promise<void>
  signOut: () => Promise<void>
  navigate: (r: Route, replace?: boolean) => void
  refreshConfig: () => Promise<void>
  refreshConversations: () => Promise<void>
  refreshProjects: () => Promise<void>
  refreshPrompts: () => Promise<void>
  refreshSkills: () => Promise<void>
  refreshConnectors: () => Promise<void>
  upsertConversation: (c: ConversationSummary) => void
  removeConversation: (id: string) => Promise<void>
  saveSettings: (patch: UserSettings) => Promise<void>
  setSidebar: (open: boolean) => void
  setSettingsOpen: (open: boolean) => void
  openArtifact: (a: Artifact | null) => void
  openCode: (v: CodeView | null) => void
  notify: (msg: string) => void
  defaultModelId: () => string | undefined
}

let toastTimer: number | undefined

let refreshTimer: number | undefined

export const useApp = create<AppState>((set, get) => ({
  user: undefined,
  config: null,
  settings: {},
  conversations: [],
  projects: [],
  prompts: [],
  skills: [],
  connectors: [],
  route: parseRoute(location.pathname),
  sidebarOpen: window.innerWidth > 900,
  settingsOpen: false,
  artifact: null,
  codeView: null,
  workspaceChoice: {},
  toast: null,

  init: async () => {
    window.addEventListener('popstate', () => set({ route: parseRoute(location.pathname), artifact: null }))
    setUnauthorizedHandler(() => {
      if (get().user) set({ user: null, conversations: [], projects: [], prompts: [], skills: [], connectors: [], settings: {}, artifact: null })
    })
    try {
      set({ user: await api.me() })
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) { set({ user: null }); return }
      throw e
    }
    await get().bootstrap()
  },

  /** Load everything that belongs to the signed-in user. */
  bootstrap: async () => {
    const [config, settings, conversations, projects, prompts, skills, connectors] = await Promise.all([
      api.config(), api.settings(), api.conversations(), api.projects(), api.prompts(), api.skills(), api.connectors(),
    ])
    set({ config, settings, conversations, projects, prompts, skills, connectors })
    // Model availability changes as sparks come and go.
    clearInterval(refreshTimer)
    refreshTimer = window.setInterval(() => void get().refreshConfig(), 30_000)
  },

  signIn: async (username, password) => {
    const user = await api.login(username, password)
    set({ user })
    await get().bootstrap()
  },

  signOut: async () => {
    clearInterval(refreshTimer)
    try { await api.logout() } finally {
      set({ user: null, conversations: [], projects: [], prompts: [], skills: [], connectors: [], settings: {}, artifact: null, config: null })
      get().navigate({ name: 'home' }, true)
    }
  },

  navigate: (r, replace = false) => {
    const path = routePath(r)
    if (path !== location.pathname) history[replace ? 'replaceState' : 'pushState'](null, '', path)
    const leavingChat = get().route.name !== 'chat' || r.name !== 'chat' || (get().route as { id: string }).id !== r.id
    set({ route: r, ...(leavingChat ? { artifact: null } : {}), ...(window.innerWidth <= 900 ? { sidebarOpen: false } : {}) })
  },

  refreshConfig: async () => {
    try { set({ config: await api.config() }) } catch { /* keep last known */ }
  },
  refreshConversations: async () => set({ conversations: await api.conversations() }),
  refreshProjects: async () => set({ projects: await api.projects() }),
  refreshPrompts: async () => set({ prompts: await api.prompts() }),
  refreshSkills: async () => set({ skills: await api.skills() }),
  refreshConnectors: async () => set({ connectors: await api.connectors() }),

  upsertConversation: (c) => set((s) => {
    const rest = s.conversations.filter((x) => x.id !== c.id)
    const merged = { ...s.conversations.find((x) => x.id === c.id), ...c }
    return { conversations: [merged, ...rest].sort((a, b) => b.pinned - a.pinned || b.updated_at - a.updated_at) }
  }),

  removeConversation: async (id) => {
    await api.deleteConversation(id)
    set((s) => ({ conversations: s.conversations.filter((c) => c.id !== id) }))
    const r = get().route
    if (r.name === 'chat' && r.id === id) get().navigate({ name: 'home' }, true)
  },

  saveSettings: async (patch) => {
    set((s) => ({ settings: { ...s.settings, ...patch } }))
    await api.saveSettings(patch)
  },

  setSidebar: (open) => set({ sidebarOpen: open }),
  setSettingsOpen: (open) => set({ settingsOpen: open }),
  openArtifact: (a) => set({ artifact: a }),
  openCode: (codeView) => set({ codeView }),
  chooseWorkspace: (projectId, workspaceId) => set((s) => ({ workspaceChoice: { ...s.workspaceChoice, [projectId]: workspaceId } })),
  notify: (msg) => {
    clearTimeout(toastTimer)
    set({ toast: msg })
    toastTimer = window.setTimeout(() => set({ toast: null }), 4000)
  },

  defaultModelId: () => {
    const { config, settings } = get()
    const preferred = settings.model_id as string | undefined
    if (preferred && config?.models.some((m) => m.id === preferred)) return preferred
    return config?.models.find((m) => m.default)?.id ?? config?.models[0]?.id
  },
}))
