import { memo } from 'react'

type Row = { kind: 'add' | 'del' | 'ctx' | 'hunk' | 'meta'; text: string; oldNo?: number; newNo?: number }

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/

/** Unified diff → rows with old/new line numbers. File headers (diff --git, index, ---/+++) are dropped. */
function parse(diff: string): Row[] {
  const rows: Row[] = []
  let o = 0
  let n = 0
  for (const line of diff.split('\n')) {
    const h = HUNK.exec(line)
    if (h) {
      o = Number(h[1]); n = Number(h[2])
      rows.push({ kind: 'hunk', text: line })
    } else if (line.startsWith('diff --git') || line.startsWith('index ') || line.startsWith('--- ') || line.startsWith('+++ ')
      || line.startsWith('new file mode') || line.startsWith('deleted file mode') || line.startsWith('similarity index')) {
      continue
    } else if (line.startsWith('rename from') || line.startsWith('rename to') || line.startsWith('Binary files') || line.startsWith('\\ ')) {
      rows.push({ kind: 'meta', text: line })
    } else if (line.startsWith('+')) {
      rows.push({ kind: 'add', text: line.slice(1), newNo: n++ })
    } else if (line.startsWith('-')) {
      rows.push({ kind: 'del', text: line.slice(1), oldNo: o++ })
    } else if (line.startsWith(' ')) {
      rows.push({ kind: 'ctx', text: line.slice(1), oldNo: o++, newNo: n++ })
    }
  }
  return rows
}

function DiffViewImpl({ diff, empty = 'No changes' }: { diff: string; empty?: string }) {
  const rows = parse(diff)
  if (!rows.length) return <div className="diff-empty muted small">{empty}</div>
  return (
    <div className="diff" role="table">
      {rows.map((r, i) => (
        <div key={i} className={`diff-row ${r.kind}`} role="row">
          <span className="diff-no">{r.oldNo ?? ''}</span>
          <span className="diff-no">{r.newNo ?? ''}</span>
          <span className="diff-sign">{r.kind === 'add' ? '+' : r.kind === 'del' ? '−' : ''}</span>
          <span className="diff-text">{r.text || ' '}</span>
        </div>
      ))}
    </div>
  )
}

export const DiffView = memo(DiffViewImpl)
