import type {
  ActivityEntry, AdminConnector, HealthReport, AdminUser, ApiKey, AppConfig, Connector, Attachment, Conversation, ConversationSummary, Project, ProjectDetail, PromptShortcut,
  PushInfo, Repo, RepoFile, ShareLink, SharedChat, Skill, StreamEvent, Workspace, WorkspaceChanges, TeamAccess, Usage, User, UserSettings,
} from './types'

type PromptFields = Pick<PromptShortcut, 'command' | 'title' | 'description' | 'body'>
type SkillFields = Pick<Skill, 'name' | 'title' | 'description' | 'body'>
export type ConnectorFields = Pick<AdminConnector, 'label' | 'description' | 'url' | 'transport'> & { headers?: Record<string, string> }

export class ApiError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

/** Called when any request finds the session gone (expired, revoked, signed out elsewhere). */
let onUnauthorized: () => void = () => {}
export const setUnauthorizedHandler = (fn: () => void) => { onUnauthorized = fn }

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = init.body && !(init.body instanceof FormData) ? { 'Content-Type': 'application/json' } : undefined
  const res = await fetch(`/api${path}`, { ...init, headers: { ...headers, ...init.headers } })
  if (res.status === 401 && !path.startsWith('/auth/')) onUnauthorized()
  if (!res.ok) {
    let msg = res.statusText
    try {
      const j = await res.json()
      msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail ?? j)
    } catch { /* non-JSON error body */ }
    throw new ApiError(res.status, msg)
  }
  return res.status === 204 ? (undefined as T) : res.json()
}

const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) })

export const api = {
  me: () => req<User>('/auth/me'),
  login: (username: string, password: string) => req<User>('/auth/login', json('POST', { username, password })),
  logout: () => req<void>('/auth/logout', { method: 'POST' }),
  changePassword: (current: string, next: string) => req<void>('/auth/password', json('POST', { current, new: next })),

  admin: {
    users: () => req<AdminUser[]>('/admin/users'),
    createUser: (body: { username: string; display_name: string; password: string; role: 'admin' | 'user' }) =>
      req<User>('/admin/users', json('POST', body)),
    updateUser: (id: string, body: Partial<{ display_name: string; role: 'admin' | 'user'; disabled: boolean; password: string }>) =>
      req<User>(`/admin/users/${id}`, json('PATCH', body)),
    deleteUser: (id: string) => req<void>(`/admin/users/${id}`, { method: 'DELETE' }),
    usage: (days: number) => req<Usage>(`/admin/usage?days=${days}`),
    health: () => req<HealthReport>('/admin/health'),
    keys: () => req<ApiKey[]>('/admin/keys'),
    revokeKey: (id: string) => req<void>(`/admin/keys/${id}`, { method: 'DELETE' }),
    activity: (p: { before?: number; category?: string; q?: string }) => {
      const qs = new URLSearchParams({ limit: '100' })
      if (p.before) qs.set('before', String(p.before))
      if (p.category) qs.set('category', p.category)
      if (p.q) qs.set('q', p.q)
      return req<{ items: ActivityEntry[]; next: number | null }>(`/admin/activity?${qs}`)
    },
  },

  keys: () => req<ApiKey[]>('/keys'),
  createKey: (name: string) => req<ApiKey & { key: string }>('/keys', json('POST', { name })),
  revokeKey: (id: string) => req<void>(`/keys/${id}`, { method: 'DELETE' }),

  prompts: () => req<PromptShortcut[]>('/prompts'),
  createPrompt: (body: PromptFields & { scope: 'team' | 'personal' }) => req<PromptShortcut>('/prompts', json('POST', body)),
  updatePrompt: (id: string, body: Partial<PromptFields>) => req<PromptShortcut>(`/prompts/${id}`, json('PATCH', body)),
  deletePrompt: (id: string) => req<void>(`/prompts/${id}`, { method: 'DELETE' }),

  skills: () => req<Skill[]>('/skills'),
  createSkill: (body: SkillFields & { scope: 'team' | 'personal' }) => req<Skill>('/skills', json('POST', body)),
  updateSkill: (id: string, body: Partial<SkillFields>) => req<Skill>(`/skills/${encodeURIComponent(id)}`, json('PATCH', body)),
  deleteSkill: (id: string) => req<void>(`/skills/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  connectors: () => req<Connector[]>('/connectors'),
  adminConnectors: () => req<AdminConnector[]>('/admin/connectors'),
  createConnector: (body: ConnectorFields & { name: string }) => req<AdminConnector>('/admin/connectors', json('POST', body)),
  updateConnector: (id: string, body: Partial<ConnectorFields> & { enabled?: boolean; disabled_tools?: string[] }) =>
    req<AdminConnector>(`/admin/connectors/${id}`, json('PATCH', body)),
  refreshConnector: (id: string) => req<AdminConnector>(`/admin/connectors/${id}/refresh`, { method: 'POST' }),
  deleteConnector: (id: string) => req<void>(`/admin/connectors/${id}`, { method: 'DELETE' }),

  config: () => req<AppConfig>('/config'),
  settings: () => req<UserSettings>('/settings'),
  saveSettings: (s: UserSettings) => req<UserSettings>('/settings', json('PUT', s)),

  conversations: (params: { q?: string; project_id?: string } = {}) => {
    const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v) as [string, string][])
    return req<ConversationSummary[]>(`/conversations${qs.size ? `?${qs}` : ''}`)
  },
  conversation: (id: string) => req<Conversation>(`/conversations/${id}`),
  createConversation: (body: { model_id?: string; project_id?: string | null; workspace_id?: string | null }) =>
    req<ConversationSummary>('/conversations', json('POST', body)),
  patchConversation: (id: string, body: Partial<Pick<Conversation, 'title' | 'model_id' | 'system_prompt'>> & { pinned?: boolean }) =>
    req<ConversationSummary>(`/conversations/${id}`, json('PATCH', body)),
  deleteConversation: (id: string) => req<void>(`/conversations/${id}`, { method: 'DELETE' }),
  workspaces: (pid: string) => req<Workspace[]>(`/projects/${pid}/workspaces`),
  createWorkspace: (pid: string, body: { name?: string; repo_id?: string | null }) =>
    req<Workspace>(`/projects/${pid}/workspaces`, json('POST', body)),
  workspace: (wid: string) => req<Workspace>(`/workspaces/${wid}`),
  deleteWorkspace: (wid: string) => req<void>(`/workspaces/${wid}`, { method: 'DELETE' }),
  workspaceChanges: (wid: string) => req<WorkspaceChanges>(`/workspaces/${wid}/changes`),
  workspaceDiff: (wid: string, path: string) =>
    req<{ diff: string; truncated: boolean }>(`/workspaces/${wid}/diff?path=${encodeURIComponent(path)}`),
  discardWorkspace: (wid: string, path?: string) => req<WorkspaceChanges>(`/workspaces/${wid}/discard`, json('POST', { path: path ?? null })),
  commitWorkspace: (wid: string, body: { message: string; push: boolean; branch?: string }) =>
    req<{ sha: string; pushed: PushInfo | null; workspace: Workspace }>(`/workspaces/${wid}/commit`, json('POST', body)),
  pushWorkspace: (wid: string, branch: string) =>
    req<{ pushed: PushInfo; workspace: Workspace }>(`/workspaces/${wid}/push`, json('POST', { branch })),
  workspacePatchUrl: (wid: string) => `/api/workspaces/${wid}/patch`,
  workspaceArchiveUrl: (wid: string) => `/api/workspaces/${wid}/archive`,
  shareOf: (cid: string) => req<ShareLink | null>(`/conversations/${cid}/share`),
  saveShare: (cid: string) => req<ShareLink>(`/conversations/${cid}/share`, { method: 'POST' }),
  deleteShare: (cid: string) => req<void>(`/conversations/${cid}/share`, { method: 'DELETE' }),
  shares: () => req<ShareLink[]>('/shares'),
  sharedChat: (token: string) => req<SharedChat>(`/shares/${encodeURIComponent(token)}`),

  projects: () => req<Project[]>('/projects'),
  project: (id: string) => req<ProjectDetail>(`/projects/${id}`),
  createProject: (body: { name: string; description?: string; instructions?: string; team_access?: TeamAccess }) =>
    req<Project>('/projects', json('POST', body)),
  patchProject: (id: string, body: Partial<Pick<Project, 'name' | 'description' | 'instructions' | 'team_access'>>) =>
    req<Project>(`/projects/${id}`, json('PATCH', body)),
  deleteProject: (id: string) => req<void>(`/projects/${id}`, { method: 'DELETE' }),
  uploadProjectFile: (id: string, file: File) => upload(`/projects/${id}/files`, file),
  deleteProjectFile: (pid: string, aid: string) => req<void>(`/projects/${pid}/files/${aid}`, { method: 'DELETE' }),

  repos: (pid: string) => req<Repo[]>(`/projects/${pid}/repos`),
  addGitRepo: (pid: string, body: { url: string; ref?: string; token?: string }) =>
    req<Repo>(`/projects/${pid}/repos/git`, json('POST', body)),
  addZipRepo: (pid: string, file: File) => uploadAs<Repo>(`/projects/${pid}/repos/zip`, file, 'POST'),
  replaceZipRepo: (pid: string, rid: string, file: File) => uploadAs<Repo>(`/projects/${pid}/repos/${rid}/zip`, file, 'PUT'),
  syncRepo: (pid: string, rid: string) => req<Repo>(`/projects/${pid}/repos/${rid}/sync`, { method: 'POST' }),
  deleteRepo: (pid: string, rid: string) => req<void>(`/projects/${pid}/repos/${rid}`, { method: 'DELETE' }),
  repoFile: (rid: string, path: string) => req<RepoFile>(`/repos/${rid}/file?path=${encodeURIComponent(path)}`),

  upload: (file: File) => upload('/uploads', file),
  attachmentUrl: (id: string) => `/api/attachments/${id}/content`,
  attachmentText: (id: string) => req<{ text: string }>(`/attachments/${id}/text`),

  transcribe: async (audio: Blob) => {
    const fd = new FormData()
    fd.append('file', audio, 'speech.wav')
    return (await req<{ text: string }>('/transcribe', { method: 'POST', body: fd })).text
  },
}

function uploadAs<T>(path: string, file: File, method: 'POST' | 'PUT') {
  const fd = new FormData()
  fd.append('file', file, file.name)
  return req<T>(path, { method, body: fd })
}

function upload(path: string, file: File) {
  const fd = new FormData()
  fd.append('file', file, file.name)
  return req<Attachment>(path, { method: 'POST', body: fd })
}

export interface SendBody {
  content?: string
  attachment_ids?: string[]
  model_id?: string
  thinking?: boolean
  web_search?: boolean
  analysis?: boolean
  prompt_id?: string
  skill_id?: string
  connectors?: string[]
  edit_message_id?: string
  regenerate?: boolean
}

/** POST a message and yield parsed server-sent events until the stream ends. */
export async function* streamMessage(conversationId: string, body: SendBody, signal: AbortSignal):
  AsyncGenerator<StreamEvent> {
  const res = await fetch(`/api/conversations/${conversationId}/messages`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal,
  })
  if (!res.ok || !res.body) {
    if (res.status === 401) onUnauthorized()
    let msg = res.statusText
    try { msg = (await res.json()).detail ?? msg } catch { /* ignore */ }
    throw new ApiError(res.status, msg)
  }
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += value
    let sep: number
    while ((sep = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, sep)
      buf = buf.slice(sep + 2)
      for (const line of frame.split('\n')) {
        if (line.startsWith('data:')) yield JSON.parse(line.slice(5)) as StreamEvent
      }
    }
  }
}

/** Streams synthesized speech as raw PCM; returns the response for the player. */
export async function fetchSpeech(text: string, voiceId: string | undefined, signal: AbortSignal) {
  const res = await fetch('/api/speak', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, voice_id: voiceId }), signal,
  })
  if (!res.ok || !res.body) throw new ApiError(res.status, (await res.text()) || 'Speech failed')
  return { rate: Number(res.headers.get('X-Sample-Rate') || 24000), body: res.body }
}
