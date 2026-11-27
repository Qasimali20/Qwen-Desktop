import { useEffect, useState } from 'react'
import { WORKING_WORDS } from '../lib/phrases'
import { WORD_ROTATE_MS } from '../lib/useWorkingWord'

const SHOW_ELAPSED_AFTER_S = 4

/** Animated status while Qwen is busy: a specific `label` (e.g. "Searching knowledge base…") when the
 *  server sends one, otherwise a rotating word. Shows elapsed seconds once a wait gets noticeable. */
export function WorkingLine({ label }: { label?: string | null }) {
  const [start] = useState(() => Date.now())
  const [offset] = useState(() => Math.floor(Math.random() * WORKING_WORDS.length))
  const [elapsedMs, setElapsedMs] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setElapsedMs(Date.now() - start), 1000)
    return () => clearInterval(t)
  }, [start])
  const elapsed = Math.floor(elapsedMs / 1000)
  const word = WORKING_WORDS[(offset + Math.floor(elapsedMs / WORD_ROTATE_MS)) % WORKING_WORDS.length]
  return (
    <div className="status-line" role="status" aria-live="polite">
      <span className="pulse" />
      <span key={label ?? word} className="status-word">{label ?? `${word}…`}</span>
      {elapsed >= SHOW_ELAPSED_AFTER_S && <span className="status-elapsed">{elapsed}s</span>}
    </div>
  )
}
