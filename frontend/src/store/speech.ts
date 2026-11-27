import { create } from 'zustand'
import { fetchSpeech } from '../api/client'
import { PcmPlayer } from '../lib/audio'
import { toSpeech } from '../lib/text'
import { useApp } from './app'

interface SpeechState {
  /** id of the message being spoken, and whether audio is still loading */
  speakingId: string | null
  loading: boolean
  speak: (id: string, markdown: string) => Promise<void>
  stop: () => void
}

let current: { abort: AbortController; player?: PcmPlayer } | null = null

export const useSpeech = create<SpeechState>((set, get) => ({
  speakingId: null,
  loading: false,

  speak: async (id, markdown) => {
    get().stop()
    const text = toSpeech(markdown)
    if (!text) return
    const abort = new AbortController()
    const session: { abort: AbortController; player?: PcmPlayer } = { abort }
    current = session
    set({ speakingId: id, loading: true })
    try {
      const { rate, body } = await fetchSpeech(text, useApp.getState().settings.voice_id, abort.signal)
      session.player = new PcmPlayer(rate)
      const reader = body.getReader()
      let first = true
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        session.player.push(value)
        if (first) { first = false; set({ loading: false }) }
      }
      await session.player.drain()
    } catch (e) {
      if ((e as Error).name !== 'AbortError') useApp.getState().notify(`Speech failed: ${(e as Error).message}`)
    } finally {
      if (current === session) {
        session.player?.stop()
        current = null
        set({ speakingId: null, loading: false })
      }
    }
  },

  stop: () => {
    if (!current) return
    current.abort.abort()
    current.player?.stop()
    current = null
    set({ speakingId: null, loading: false })
  },
}))
