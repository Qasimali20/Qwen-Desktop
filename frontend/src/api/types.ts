export interface ModelInfo {
  id: string
  label: string
  description: string
  context: number
  tools: boolean
  thinking: boolean
  vision: boolean
  default: boolean
  online: boolean
  replicas?: number
  total?: number
}

export interface AppConfig {
  models: ModelInfo[]
  voices: { id: string; label: string }[]
  features: {
    asr: boolean; ocr: boolean; vision_routing: boolean; web_search: string; analysis: boolean; knowledge: boolean
    gateway: boolean
    repo_sync_minutes: number   // 0: git repositories only sync on request
  }
  gateway: { anthropic_default_model: string; public_url: string | null; http_port: number }
  /** Model new workspace chats start on; null: the usual default. */
  workspace_model: string | null
}

export interface Attachment {
  id: string
  project_id: string | null
  filename: string
  mime: string
  kind: 'image' | 'document'
  size: number
  meta: {
    width?: number; height?: number; pages?: number; ocr_pages?: number; chars?: number; truncated?: boolean
    kb?: { status: 'indexing' | 'ready' | 'error'; chunks?: number; error?: string }
    uploaded_by?: string
    secrets?: { kind: string; label: string; line: number }[]
  }
  created_at: number
}

/** A cited passage: a document page (attachment_id) or a code range (kind 'code', repo_id + lines). */
export interface Source {
  n: number; filename: string; page: number | null; snippet: string
  attachment_id?: string
  kind?: 'code'; repo_id?: string; start_line?: number; end_line?: number
}

export type RepoStatus = 'queued' | 'fetching' | 'indexing' | 'ready' | 'error'
export interface Repo {
  id: string
  project_id: string
  name: string
  source: 'zip' | 'git'
  url: string | null
  ref: string | null
  commit_sha: string | null
  status: RepoStatus
  auto_sync: number            // git only: 1 = checked for new commits on a schedule
  checked_at: number | null    // last remote check (automatic sync)
  meta: {
    files?: number; indexed?: number; chunks?: number; bytes?: number; lines?: number; langs?: string[]
    skipped?: number; error?: string | null; synced_at?: number
    auto_sync_error?: string | null; sync_trigger?: 'auto' | 'manual'
    changes?: { added: number; changed: number; removed: number; unchanged: number }
  }
  added_by: string | null
  created_at: number
  updated_at: number
}
export interface RepoFile { path: string; lang: string | null; size: number; lines: number; text: string; repo: string; commit_sha: string | null }
export interface OutputFile { id: string; filename: string; mime: string; kind: 'image' | 'document'; size: number }

export type Block =
  | { type: 'thinking'; text: string }
  | { type: 'text'; text: string }
  | {
    type: 'tool'; call_id: string; name: string; arguments: string; result: string | null; ok: boolean | null
    sources?: Source[]; outputs?: OutputFile[]
    /** write_file / edit_file in a workspace: what changed. */
    file?: { path: string; created: boolean; added: number; removed: number; diff: string }
    /** use_skill: the playbook that was loaded. */
    skill?: { name: string; title: string }
    /** MCP connector tool call. */
    connector?: { server: string; tool: string }
  }

export interface Message {
  id: string
  conversation_id: string
  seq: number
  role: 'user' | 'assistant'
  content: string
  blocks: Block[]
  attachment_ids: string[]
  attachments?: Attachment[]
  model_id: string | null
  usage: { prompt_tokens?: number; completion_tokens?: number; duration_s?: number; error?: string } | null
  meta?: { prompt?: { id: string; command: string; title: string }; skill?: { id: string; name: string; title: string } }
  created_at: number
}

export interface ConversationSummary {
  id: string
  title: string
  model_id: string | null
  project_id: string | null
  pinned: number
  created_at: number
  updated_at: number
}

/** none = private to the owner · view = the team can chat with it · edit = the team can also change it */
export type TeamAccess = 'none' | 'view' | 'edit'

export interface Project {
  id: string
  name: string
  description: string
  instructions: string
  team_access: TeamAccess
  user_id: string
  owner_name?: string
  permissions?: { owner: boolean; manage: boolean; edit: boolean }
  created_at: number
  updated_at: number
  chat_count?: number
  file_count?: number
}

/** Rolling summary of the turns that no longer fit the model's context (backend/app/summary.py). */
export interface ContextSummary {
  text: string
  upto_id: string     // last message the summary covers
  upto_seq: number
  count: number       // messages folded into it so far
  at: number
}

export interface Conversation extends ConversationSummary {
  system_prompt: string
  context_summary?: ContextSummary | null
  messages: Message[]
  project: Project | null
  workspace_id?: string | null
  workspace?: { id: string; name: string; status: Workspace['status'] } | null
}

/** A writable working copy of project code on the server that Qwen edits and runs commands in. */
export interface Workspace {
  id: string
  project_id: string
  name: string
  repo_id: string | null
  repo: { id: string; name: string; source: 'zip' | 'git'; url: string | null } | null
  source: 'repo' | 'empty'
  base_ref: string | null
  base_sha: string | null
  status: 'creating' | 'ready' | 'error'
  meta: { error?: string; pushed?: PushInfo }
  can_push: boolean
  chats?: number
  created_at: number
  updated_at: number
}

export interface PushInfo { branch: string; sha: string; at: number; url: string | null }

export interface WorkspaceChange {
  path: string
  status: 'A' | 'M' | 'D' | 'R' | 'T' | 'C' | 'U'
  added: number | null
  removed: number | null
  old_path: string | null
}

export interface WorkspaceChanges { head: string; base_sha: string | null; files: WorkspaceChange[]; truncated: boolean }

/** A conversation's read-only link (the id is the link token). */
export interface ShareLink {
  id: string
  conversation_id: string
  title: string
  views: number
  message_count: number
  created_at: number
  updated_at: number
}

/** A shared conversation as seen through its link: a snapshot of the messages. */
export interface SharedChat {
  id: string
  title: string
  owner: string
  is_owner: boolean
  conversation_id: string
  updated_at: number
  messages: Message[]
}

export interface ProjectDetail extends Project {
  files: Attachment[]
  repos: Repo[]
  conversations: ConversationSummary[]
}

export interface UserSettings {
  custom_instructions?: string
  voice_id?: string
  theme?: 'system' | 'light' | 'dark'
  thinking?: boolean
  web_search?: boolean
  analysis?: boolean
  auto_speak?: boolean
  /** MCP connector ids switched on for chats. */
  connectors?: string[]
  [k: string]: unknown
}

export type StreamEvent =
  | { type: 'user_message'; message: Message }
  | { type: 'assistant_start'; id: string; model_id: string }
  | { type: 'block_start'; index: number; block: Block }
  | { type: 'delta'; index: number; text: string }
  | { type: 'tool_result'; index: number; result: string; ok: boolean; sources?: Source[]; outputs?: OutputFile[] }
  | { type: 'status'; text: string }
  | { type: 'title'; title: string }
  | ({ type: 'summary' } & ContextSummary)
  | { type: 'done'; message: Message }
  | { type: 'error'; message: string; saved?: Message | null }

export interface User {
  id: string
  username: string
  display_name: string
  role: 'admin' | 'user'
  disabled: number
  created_at: number
  last_login_at: number | null
}

export interface AdminUser extends User {
  chat_count: number
  last_seen_at: number | null
}

export interface UsageRow { replies: number; prompt_tokens: number; completion_tokens: number }

export interface Usage {
  days: number
  by_user: (UsageRow & { user_id: string })[]
  by_model: (UsageRow & { model_id: string | null })[]
  by_source: (UsageRow & { source: 'app' | 'api' })[]
  by_day: (UsageRow & { day: string })[]
}

/** An expert playbook the model loads (use_skill) or the user picks in the composer. */
export interface Skill {
  id: string
  name: string
  title: string
  description: string
  body: string
  scope: 'builtin' | 'team' | 'personal'
  editable: boolean
}

/** An MCP server a user can switch on for their chats. */
export interface Connector {
  id: string
  name: string
  label: string
  description: string
  status: 'unknown' | 'ok' | 'error'
  tool_count: number
}

export interface ConnectorTool {
  name: string
  title: string | null
  description: string
  read_only: boolean
  destructive: boolean
  enabled: boolean
}

export interface AdminConnector extends Connector {
  url: string
  transport: 'http' | 'sse'
  enabled: boolean
  header_names: string[]
  error: string | null
  checked_at: number | null
  updated_at: number
  tools: ConnectorTool[]
}

/** One entry of the admin activity log. */
export type HealthStatus = 'ok' | 'warn' | 'error'

export interface HealthCheck {
  id: string
  group: string
  label: string
  status: HealthStatus
  detail: string
  ms?: number
}

export interface HealthReport {
  status: HealthStatus
  checked_at: number
  ms: number
  checks: HealthCheck[]
}

export interface ActivityEntry {
  id: number
  at: number
  actor_id: string | null
  actor_name: string
  action: string
  target_type: string | null
  target_id: string | null
  target_label: string | null
  details: Record<string, unknown>
  ip: string | null
}

export interface PromptShortcut {
  id: string
  command: string
  title: string
  description: string
  body: string
  scope: 'team' | 'personal'
  editable: boolean
  created_at: number
  updated_at: number
}

export interface ApiKey {
  id: string
  user_id: string
  name: string
  prefix: string
  created_at: number
  last_used_at: number | null
  revoked_at: number | null
  /** only present right after creation */
  key?: string
  /** admin listing */
  display_name?: string
  username?: string
}
