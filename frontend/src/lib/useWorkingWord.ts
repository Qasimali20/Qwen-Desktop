import { useEffect, useState } from 'react'
import { WORKING_WORDS } from './phrases'

export const WORD_ROTATE_MS = 2600

/** A rotating "working" word (e.g. for the live thinking header); rotates only while `active`. */
export function useWorkingWord(active: boolean): string {
  const [offset] = useState(() => Math.floor(Math.random() * WORKING_WORDS.length))
  const [tick, setTick] = useState(0)
  useEffect(() => {
    if (!active) return
    const t = setInterval(() => setTick((n) => n + 1), WORD_ROTATE_MS)
    return () => clearInterval(t)
  }, [active])
  return WORKING_WORDS[(offset + tick) % WORKING_WORDS.length]
}
