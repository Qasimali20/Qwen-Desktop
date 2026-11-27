import { LogoMark } from './LogoMark'
import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronsUpDown, FolderOpen, GraduationCap, KeyRound, LogOut, MessageSquare, MoreHorizontal, PanelLeftClose, Pin, PinOff, Plus, Search, Settings, ShieldCheck, Trash2, X, Zap } from 'lucide-react'
import { api } from '../api/client'
import type { ConversationSummary } from '../api/types'
import { relativeDay } from '../lib/text'
import { useApp } from '../store/app'

function ItemMenu({ c, onClose }: { c: ConversationSummary; onClose: () => void }) {
  const { upsertConversation, removeConversation } = useApp()
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const h = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && onClose()
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [onClose])
  return (
    <div className="menu item-menu" ref={ref}>
      <button className="menu-item" onClick={async () => { upsertConversation(await api.patchConversation(c.id, { pinned: !c.pinned })); onClose() }}>
        {c.pinned ? <PinOff size={14} /> : <Pin size={14} />} {c.pinned ? 'Unpin' : 'Pin'}
      </button>
      <button className="menu-item danger" onClick={() => { onClose(); if (confirm(`Delete “${c.title}”?`)) void removeConversation(c.id) }}>
        <Trash2 size={14} /> Delete
      </button>
    </div>
  )
}

function UserMenu() {
  const { user, config, setSettingsOpen, navigate, signOut } = useApp()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const h = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false)
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [open])
  if (!user) return null
  return (
    <div className="user-menu" ref={ref}>
      {open && (
        <div className="menu user-dropdown">
          <div className="menu-label">{user.username}</div>
          <button className="menu-item" onClick={() => { setOpen(false); setSettingsOpen(true) }}><Settings size={14} /> Settings</button>
          {config?.features.gateway && (
            <button className="menu-item" onClick={() => { setOpen(false); navigate({ name: 'developer' }) }}><KeyRound size={14} /> API keys &amp; IDE setup</button>
          )}
          {user.role === 'admin' && (
            <button className="menu-item" onClick={() => { setOpen(false); navigate({ name: 'admin' }) }}><ShieldCheck size={14} /> Admin console</button>
          )}
          <button className="menu-item" onClick={() => { setOpen(false); void signOut() }}><LogOut size={14} /> Sign out</button>
        </div>
      )}
      <button className="user-btn" onClick={() => setOpen(!open)}>
        <span className="initials">{user.display_name.slice(0, 2).toUpperCase()}</span>
        <span className="user-text">
          <span className="user-name">{user.display_name}</span>
          <span className="user-role">{user.role === 'admin' ? 'Administrator' : 'Member'}</span>
        </span>
        <ChevronsUpDown size={14} className="muted" />
      </button>
    </div>
  )
}

export function Sidebar() {
  const { conversations, route, navigate, setSidebar, sidebarOpen } = useApp()
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<ConversationSummary[] | null>(null)
  const [menuFor, setMenuFor] = useState<string | null>(null)

  // Debounced full-text search across titles and message bodies.
  useEffect(() => {
    const q = query.trim()
    if (!q) { setResults(null); return }
    const t = setTimeout(() => void api.conversations({ q }).then(setResults), 200)
    return () => clearTimeout(t)
  }, [query])

  const groups = useMemo(() => {
    const list = results ?? conversations
    const out: [string, ConversationSummary[]][] = []
    const pinned = list.filter((c) => c.pinned)
    if (pinned.length && !results) out.push(['Pinned', pinned])
    for (const c of results ? list : list.filter((x) => !x.pinned)) {
      const label = relativeDay(c.updated_at)
      const g = out.at(-1)
      if (g && g[0] === label) g[1].push(c)
      else out.push([label, [c]])
    }
    return out
  }, [conversations, results])

  const activeId = route.name === 'chat' ? route.id : null

  return (
    <>
      <div className={`scrim${sidebarOpen ? ' show' : ''}`} onClick={() => setSidebar(false)} />
      <nav className={`sidebar${sidebarOpen ? '' : ' closed'}`}>
        <div className="sb-top">
          <button className="brand" onClick={() => navigate({ name: 'home' })}>
            <LogoMark />
            <span className="brand-text">
              <span className="brand-name">Qwen Desktop</span>
              <span className="brand-kicker">Sovereign AI Workspace</span>
            </span>
          </button>
          <button className="icon-btn" title="Close sidebar" onClick={() => setSidebar(false)}><PanelLeftClose size={18} /></button>
        </div>
        <button className="new-chat" onClick={() => navigate({ name: 'home' })}>
          <span className="plus"><Plus size={16} /></span> New chat
        </button>
        <div className="sb-nav">
          <button className={route.name === 'home' && !('projectId' in route && route.projectId) ? 'on' : ''} onClick={() => navigate({ name: 'home' })}>
            <MessageSquare size={16} /> Chats
          </button>
          <button className={route.name === 'projects' || route.name === 'project' ? 'on' : ''} onClick={() => navigate({ name: 'projects' })}>
            <FolderOpen size={16} /> Projects
          </button>
          <button className={route.name === 'shortcuts' ? 'on' : ''} onClick={() => navigate({ name: 'shortcuts' })}>
            <Zap size={16} /> Shortcuts
          </button>
          <button className={route.name === 'skills' ? 'on' : ''} onClick={() => navigate({ name: 'skills' })}>
            <GraduationCap size={16} /> Skills
          </button>
        </div>
        <div className="sb-search">
          <Search size={14} />
          <input placeholder="Search chats" value={query} onChange={(e) => setQuery(e.target.value)} />
          {query && <button className="icon-btn sm" onClick={() => setQuery('')}><X size={12} /></button>}
        </div>
        <div className="sb-list">
          {groups.map(([label, items]) => (
            <section key={label}>
              <h4>{label}</h4>
              {items.map((c) => (
                <div key={c.id} className={`sb-item${c.id === activeId ? ' active' : ''}`}>
                  <button className="sb-link" onClick={() => navigate({ name: 'chat', id: c.id })} title={c.title}>
                    {c.title}
                  </button>
                  <button className="icon-btn sm sb-more" onClick={() => setMenuFor(menuFor === c.id ? null : c.id)}>
                    <MoreHorizontal size={14} />
                  </button>
                  {menuFor === c.id && <ItemMenu c={c} onClose={() => setMenuFor(null)} />}
                </div>
              ))}
            </section>
          ))}
          {results && !results.length && <p className="sb-empty">No chats match “{query}”.</p>}
          {!results && !conversations.length && <p className="sb-empty">Your chats will appear here.</p>}
        </div>
        <div className="sb-foot">
          <UserMenu />
        </div>
      </nav>
    </>
  )
}
