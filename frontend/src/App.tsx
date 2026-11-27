import { LogoMark } from './components/LogoMark'
import { CodeViewer } from './components/CodeViewer'
import { useEffect, useState } from 'react'
import { PanelLeftOpen, SquarePen } from 'lucide-react'
import { AdminView } from './components/Admin'
import { ArtifactPanel } from './components/ArtifactPanel'
import { ChatView } from './components/ChatView'
import { DeveloperView } from './components/Developer'
import { Login } from './components/Login'
import { ProjectsView, ProjectView } from './components/Projects'
import { SettingsDialog } from './components/SettingsDialog'
import { SharedView } from './components/Share'
import { ShortcutsView } from './components/Shortcuts'
import { SkillsView } from './components/Skills'
import { Sidebar } from './components/Sidebar'
import { useApp } from './store/app'
import { useChat } from './store/chat'

function useTheme() {
  const theme = useApp((s) => s.settings.theme ?? 'system')
  useEffect(() => {
    const root = document.documentElement
    if (theme === 'system') root.removeAttribute('data-theme')
    else root.setAttribute('data-theme', theme)
  }, [theme])
}

export function App() {
  const { init, user, config, route, sidebarOpen, setSidebar, navigate, artifact, toast } = useApp()
  const [ready, setReady] = useState(false)
  const [fatal, setFatal] = useState<string | null>(null)
  useTheme()

  useEffect(() => {
    init().then(() => setReady(true)).catch((e) => setFatal((e as Error).message))
  }, [init])

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey
      if (mod && e.shiftKey && e.key.toLowerCase() === 'o') { e.preventDefault(); navigate({ name: 'home' }) }
      if (mod && e.key === '.') { e.preventDefault(); setSidebar(!useApp.getState().sidebarOpen) }
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [navigate, setSidebar])

  // Drop the previous user's open conversation from memory on sign-out.
  useEffect(() => { if (user === null) useChat.getState().reset() }, [user])

  if (fatal) return <div className="fatal">Cannot reach the Qwen Desktop API: {fatal}</div>
  if (ready && user === null) return <><Login />{toast && <div className="toast">{toast}</div>}</>
  if (!ready || !config) return <div className="boot"><LogoMark className="logo-pulse" /></div>

  return (
    <div className={`app${artifact ? ' with-artifact' : ''}`}>
      <Sidebar />
      <main className="main">
        {!sidebarOpen && (
          <div className="floating-tools">
            <button className="icon-btn" title="Open sidebar (Ctrl+.)" onClick={() => setSidebar(true)}><PanelLeftOpen size={18} /></button>
            <button className="icon-btn" title="New chat (Ctrl+Shift+O)" onClick={() => navigate({ name: 'home' })}><SquarePen size={18} /></button>
          </div>
        )}
        {route.name === 'chat' && <ChatView conversationId={route.id} />}
        {route.name === 'home' && <ChatView />}
        {route.name === 'projects' && <ProjectsView />}
        {route.name === 'project' && <ProjectView id={route.id} />}
        {route.name === 'admin' && <AdminView />}
        {route.name === 'shortcuts' && <ShortcutsView />}
        {route.name === 'skills' && <SkillsView />}
        {route.name === 'developer' && <DeveloperView />}
        {route.name === 'share' && <SharedView key={route.token} token={route.token} />}
      </main>
      <ArtifactPanel />
      <SettingsDialog />
      <CodeViewer />
      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
