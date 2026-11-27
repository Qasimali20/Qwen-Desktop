import { useState } from 'react'
import { ChevronDown, Layers } from 'lucide-react'
import type { ContextSummary } from '../api/types'
import { Markdown } from './Markdown'

/** Marks where the conversation's rolling summary ends: Qwen sees the messages above only through it. */
export function SummaryDivider({ summary }: { summary: ContextSummary }) {
  const [open, setOpen] = useState(false)
  return (
    <div className={`summary-divider${open ? ' open' : ''}`}>
      <button className="summary-divider-line" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <Layers size={13} />
        <span title="Summarised to keep the whole conversation within Qwen's context window">
          {summary.count} earlier message{summary.count === 1 ? '' : 's'} summarised
        </span>
        <ChevronDown size={13} className="summary-chevron" />
      </button>
      {open && (
        <div className="summary-body">
          <p className="summary-hint">
            To keep this long conversation within Qwen's context window, the messages above this line are sent to it
            only as this summary. Messages below it are sent word for word.
          </p>
          <Markdown text={summary.text} />
        </div>
      )}
    </div>
  )
}
