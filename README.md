# Qwen Desktop

A Claude-Desktop-style chat app for the Qwen model fleet on the DGX Spark cluster.
React 19 + TypeScript (Vite) frontend, FastAPI backend, SQLite storage.

## Run

```bash
./start.sh          # build if needed; serve http://localhost:8300 and https://<lan-ip>:8343
./start.sh dev      # backend --reload + Vite dev server on :5173 (proxies /api)
./start.sh stop
```

Use the **https** URL from other machines: browsers allow the microphone only in a
secure context. The certificate is self-signed (`data/certs/`), so accept it once.

## Features

| | |
|---|---|
| Chat | Streaming Markdown, code highlighting, KaTeX math, tables, edit & resend, retry, stop (partial replies are kept) |
| Models | Picker with live health; multiple replicas per model are round-robined with automatic failover |
| Thinking | Qwen3.6 reasoning, streamed into a collapsible "Thought process" |
| Tools | `web_search`, `fetch_url` (SSRF-guarded), `analyze_image`; agent loop up to 12 rounds |
| Vision | Images go natively to Qwen3-VL. Models without vision receive a cached Qwen3-VL description and can call `analyze_image` |
| Documents | PDF text extraction; scanned pages are OCR'd by PaddleOCR-VL; text and code files are read directly |
| Artifacts | HTML, SVG, Mermaid and long Markdown open in a resizable side panel with live preview, code view, copy and download |
| Projects | Instructions and knowledge files injected into every chat in the project; share with the team (use-only or editable) — chats stay private |
| Workspaces | Writable copies of a project's code on the server: Qwen creates and edits files and runs commands/tests; review the diff, download it, or commit & push to a branch |
| Sharing | Read-only links to a chat (a snapshot) for anyone signed in; update or revoke any time |
| Skills | Expert playbooks (frontend, backend, security audit, DevOps, MLOps, LLMOps, AIOps, AgentOps, architecture plan) loaded automatically when a request fits, or picked with `/`; team and personal skills |
| Connectors (MCP) | Admins connect remote MCP servers (streamable HTTP or SSE); people switch them on in the composer and Qwen calls their tools |
| Secret guard | Keys, tokens, passwords and private keys are caught before a message is sent (redact or send anyway) and masked in uploaded documents and indexed code |
| Shortcuts | Type `/` in the composer: `/review`, `/tests`, `/explain-error`, `/sql`, `/regex`, `/commit-msg`, … Team shortcuts are managed by admins; anyone can add personal ones |
| API gateway | OpenAI- and Anthropic-compatible `/v1` endpoint with personal API keys, for VS Code (Continue), Qwen Code, Claude Code and scripts |
| Voice | Dictation (Qwen3-ASR), read-aloud with streaming PCM playback (Qwen3-TTS / OmniVoice), and a voice mode that talks back |
| Misc | Full-text chat search, pinning, auto titles, light/dark theme, personal preferences, mobile layout |

## Accounts

Sign-in is required. Each user sees only their own chats, projects and files.

```bash
cd backend
.venv/bin/python -m app.cli create-user <username> --admin   # first user claims pre-existing data
.venv/bin/python -m app.cli reset-password <username>
.venv/bin/python -m app.cli list-users
```

Admins manage users (create, reset password, disable, delete) and see usage per user/model in
**Admin console** (user menu, bottom-left). Passwords are scrypt-hashed; sessions are HttpOnly
cookies (30-day sliding expiry) stored as SHA-256 digests; logins are throttled per user and IP.

## Knowledge base (projects)

Project documents are chunked and embedded with Qwen3-Embedding-4B (spark-2) in the background.
While a project's documents fit in ~35% of the model's context they are included in full; beyond
that the project switches to **retrieval mode**: every question triggers a hybrid search (vector +
BM25, fused with RRF) and the model can call `search_knowledge` for follow-ups. Answers cite
`[file, p. N]`; sources open the PDF at that page. Up to ~8M characters per document.

## Code repositories (projects)
A project's **Code** section takes a git URL (https; optional branch and read-only access token for private
repos) or a `.zip` upload. A background job clones/reads it, keeps source files only (skips `.git`,
`node_modules`, virtualenvs, build output, lockfiles, minified/binary files and anything in `.gitignore`;
limits: 512 KB per file, 15k files, 24 MB per repo), splits files at function/class boundaries and embeds
them on spark-2 (~25 chunks/s, so a large repo takes a few minutes; progress shows on the card).

- In chats, Qwen gets the repo layout plus tools: `search_knowledge` (meaning + keyword search over code and
  docs), `grep_code`, `list_files`, `read_file`. Answers cite `repo/path:line`; clicking a citation opens the
  file with the lines highlighted.
- **Sync** (git) pulls the latest commit and re-embeds only files whose content changed; zip repos are updated
  by uploading a new version. Tokens are stored server-side only and never returned by the API.
- Jobs survive restarts: the http and https servers share a lease in the `repos` table, and a sweeper in
  each process resumes jobs whose lease expired.
- Code blocks in answers have **Download** (file name from the block label or first-line comment) and, for
  diffs, **Patch** (apply with `git apply`).

## Code sandbox (analysis, Node, Bash)

With **Analyze** on, tool-capable models get `run_python` and `run_shell`. Code runs in a Docker
container with no network, read-only root FS, dropped capabilities, 2 GB RAM / 2 CPU / 512 PIDs and
a 120 s limit (`run_shell` may ask for up to 600 s). The image has Python 3.12 with the data stack
plus pytest and ruff, Node.js 22 with npm, TypeScript (`tsc`) and `tsx`, git, ripgrep, jq, sqlite3
and make. Attached files (CSV, Excel, Parquet, …) are mounted read-only at `./data`; each chat has a
persistent scratch folder. Charts and files written by the code appear inline and are downloadable.
Scripts reach the container through a separate read-only mount, and parallel calls never share one.
Documents: asked for a Word, PowerPoint, PDF or Excel file, the model writes it with python-docx,
python-pptx, reportlab or openpyxl and the user gets it as a download.

```bash
./sandbox/build.sh   # one-time: builds qwen-desktop-sandbox:3
```

## Workspaces (projects)

A workspace is a user's own writable working copy of project code on the server, created from a
connected repository (git clone of its branch, or the files of an uploaded .zip) or empty. Start a
chat with a workspace selected (the picker above the project composer, or the chat icon on the
workspace) and Qwen works like a developer in it: `list_files`, `read_file`, `grep`, `edit_file`
(exact string replacement), `write_file` and `run_shell` (the sandbox above, with the tree mounted
at `/work`). New workspace chats start on `workspace_model` from `config.yaml` (Qwen3 Coder) when it is
online; the user can pick another model. Edits show as diffs in the chat; the header chip counts changed files and opens the
*Changes* panel: per-file diffs, discard (file or all), download `.patch` / `.zip`, and
**Commit & push** to a new branch (never forced; needs a token with write access on the repository)
with a link to open the pull request.

Layout under `data/workspaces/<id>/`: `work/` (the files), `git/` (repository data, kept outside the
tree and mounted read-only in the sandbox, so `git status/diff/log` work there but nothing the model
writes can plant hooks or config for the server's own git commands) and `ctl/` (server scratch).
Host-side file tools reject `..` and never follow symlinks out of the tree. Workspaces are private
to their owner; deleting a project deletes everyone's workspaces in it. Limits: 20 workspaces per
user, 1 MB per written file.

## Terminal agent (Qwen Code)

For work on files on your own computer, *API keys & IDE setup* shows a one-time setup for the
[Qwen Code](https://github.com/QwenLM/qwen-code) CLI (macOS/Linux and Windows): it writes
`~/.qwen/settings.json` (auth type `openai`, model `qwen3-coder-30b`) and `~/.qwen/.env`
(`OPENAI_BASE_URL` = the gateway, `OPENAI_API_KEY` = the user's key). Qwen Code 0.28+ refuses to
start without the auth type. Then `qwen` in any project folder; it asks before edits and commands.

## Skills

Built-in playbooks live in `backend/app/skills/*.md` (frontmatter `name`, `title`, `description`, then the
Markdown body, max 12,000 chars). Admins add team skills and everyone can add personal ones on the *Skills*
page; names are unique across all three. In chats with tool-capable models the system prompt lists each
skill's name and "when to use" line, and the model loads the full playbook with `use_skill` when a request
matches. Picking a skill with `/` in the composer attaches the playbook to that message (a snapshot is
stored with the message).

## Connectors (MCP)

*Admin → Connectors* adds a remote MCP server: URL, transport (streamable HTTP or SSE) and headers such as
`Authorization: Bearer …`. The server is contacted when it is added (a wrong URL or token is reported
immediately) and its tool list is cached; *Refresh* re-reads it. Headers are stored server-side and never
returned by the API. Tools the server marks as destructive start switched off, and admins can switch any
tool off. People switch connectors on under *Connectors* in the composer; their tools then reach the model
as `mcp__<server>__<tool>` (up to 48 per chat), each call using a short-lived MCP session. The model is told
to treat results as data and to confirm before actions that change external systems. Local (stdio) servers
and OAuth sign-in are not supported yet.

## Activity log and server logs

*Admin → Activity* lists sign-ins (including failed attempts and the reason), user changes (created, role,
disabled, password reset, deleted), API keys revoked by admins, connector changes and team skill/shortcut
changes, with who, when and the client IP. Entries keep names as they were (they outlive deleted users),
never contain passwords, tokens or header values, and are pruned after 365 days (`backend/app/audit.py`).

Server logs: `data/logs/http.log` and `https.log`, appended across restarts with timestamps
(`backend/logging.yaml`); `start.sh` rotates a log over 20 MB at start-up and keeps 5 old copies.

## Sharing and the secret guard

**Share links.** *Share* in a chat header creates a link (`/share/<token>`) to a snapshot of the
chat, viewable by anyone signed in. *Update to latest* refreshes the snapshot under the same link;
*Delete link* revokes it. Files shown in a snapshot stay readable through it (and are kept until no
message or link uses them). Usage figures are not included.

**Secret guard.** `backend/app/secretscan.py` (mirrored in `frontend/src/lib/secrets.ts`) detects
private keys, cloud/API tokens (AWS, GitHub, GitLab, OpenAI, Anthropic, Slack, Stripe, Google,
Hugging Face, Qwen Desktop keys), JWTs, passwords in URLs and `password=`/`api_key:`-style
assignments, while ignoring placeholders and code such as `os.environ[...]`. The composer offers
*Redact & send*, *Send anyway* or *Edit*; uploaded documents and indexed repository files are masked
automatically (`ghp_…[REDACTED github_token]`), and attachment chips say how many were masked.

## Team features

**Shared projects.** A project's owner sets *Sharing* to Private, *Team can use* (everyone can chat
with its instructions and files) or *Team can edit* (everyone can also change them). Renaming,
sharing and deleting stay with the owner, and with admins once a project is shared. Conversations are always
private to the person who started them. Access is re-checked on every message, so making a project
private again cuts members off immediately. Deleting a project keeps other members' chats, detached.

**Prompt shortcuts.** Templates with an optional `{{input}}` placeholder (otherwise the user's text
is appended). The model receives the expanded prompt; the chat shows the `/command` chip and the
user's text. Messages snapshot the template, so editing a shortcut never rewrites history. A
personal shortcut with the same command as a team one replaces it for that user. Defaults are seeded
once on first start; admins can edit or delete them on the *Shortcuts* page.

**API gateway (`/v1`).** Users create keys under *API keys & IDE setup* (only a SHA-256 is stored;
the key is shown once). Endpoints: `GET /v1/models`, `POST /v1/chat/completions`, `/v1/completions`,
`/v1/embeddings` (OpenAI) and `POST /v1/messages` (Anthropic, for Claude Code — `claude-*` model
names map to `gateway.anthropic_default_model`). Requests fail over between replicas, are limited
per user (`max_concurrent_per_user`, queued up to `queue_timeout_s`), and usage is recorded for the
admin console (*Usage* shows chat-app vs API split; *API keys* lists and revokes keys). Disabling a
user blocks their keys. Point tools at the plain-HTTP port (`:8300`): they reject the HTTPS port's
self-signed certificate.

```yaml
gateway:                      # all optional
  enabled: true
  max_concurrent_per_user: 4  # per server process
  anthropic_default_model: qwen3.6-35b
  public_url: http://10.170.1.143:8300   # URL shown in the setup snippets
```

## Configuration — `config.yaml`

Defines the model fleet (endpoints, context, capabilities), vision/OCR/ASR/TTS endpoints and web search.
The spark-monitor agent (`http://<spark>:8765/metrics`) lists what each node serves.

**Web search:** Google and DuckDuckGo are unreachable from this network, so the default provider
is Wikipedia. For full web search set one of:

```yaml
web_search: { provider: brave,  api_key_env: BRAVE_API_KEY }    # export BRAVE_API_KEY=...
web_search: { provider: tavily, api_key_env: TAVILY_API_KEY }
web_search: { provider: searxng, url: http://localhost:8888 }
```

## Layout

```
backend/app/
  main.py        app setup; routers/ holds auth, admin, conversations, projects, prompts, files, voice,
                 keys and the /v1 gateway
  gateway.py     OpenAI/Anthropic-compatible proxy: key auth, model aliasing, failover, limits, usage
  prompts.py     prompt shortcuts: defaults, template expansion
  auth.py        password hashing, sessions, throttling, dependencies
  chat.py        prompt assembly, context budgeting, vision routing, retrieval, tool loop
  knowledge.py   chunking, embeddings, hybrid retrieval
  sandbox.py     Docker-isolated Python / shell execution (chat scratch folders and workspaces)
  workspaces.py  project workspaces: file tools, hardened host git, review, commit & push
  secretscan.py  credential detection and masking
  skills.py      skills: built-ins (skills/*.md), team/personal, use_skill
  audit.py       activity log (sign-ins, admin actions, team-wide changes)
  connectors.py  MCP connectors: tool discovery and calls (official mcp SDK)
  repos.py       repository fetch (git/zip), code-aware chunking, indexing jobs
  llm.py         pooled OpenAI-compatible client, replica failover, SSE parsing
  tools.py       tool registry and search providers
  documents.py   image normalisation, PDF/text extraction, OCR
  voice.py       ASR / TTS proxies
  db.py          SQLite (WAL) persistence
frontend/src/
  api/           typed client + SSE stream reader
  store/         zustand stores (app, chat with rAF-batched deltas, speech)
  components/    Sidebar, ChatView, Composer (with / picker), Message, Markdown, ArtifactPanel, Projects,
                 Shortcuts, Developer (API keys & IDE setup), Admin, Settings
  lib/           audio (16 kHz WAV capture, gapless PCM player), text helpers
```
claude --resume 0e7c5a67-9903-4f5a-bca0-587b0ae10168