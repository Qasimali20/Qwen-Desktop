import { create } from 'zustand'
import { api } from '../api/client'
import type { Workspace } from '../api/types'

interface WorkspaceState {
  /** The user's workspaces per project (undefined: not loaded yet). */
  byProject: Record<string, Workspace[] | undefined>
  load: (projectId: string) => Promise<Workspace[]>
  put: (ws: Workspace) => void
  remove: (projectId: string, id: string) => void
}

export const useWorkspaces = create<WorkspaceState>((set) => ({
  byProject: {},
  load: async (projectId) => {
    const list = await api.workspaces(projectId)
    set((s) => ({ byProject: { ...s.byProject, [projectId]: list } }))
    return list
  },
  put: (ws) => set((s) => {
    const list = s.byProject[ws.project_id] ?? []
    const next = list.some((w) => w.id === ws.id) ? list.map((w) => (w.id === ws.id ? ws : w)) : [ws, ...list]
    return { byProject: { ...s.byProject, [ws.project_id]: next } }
  }),
  remove: (projectId, id) => set((s) => ({
    byProject: { ...s.byProject, [projectId]: (s.byProject[projectId] ?? []).filter((w) => w.id !== id) },
  })),
}))
