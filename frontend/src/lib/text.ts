/** Markdown → speakable plain text: drops code, tables, URLs and formatting marks. */
export function toSpeech(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, ' (code omitted) ')
    .replace(/\$\$[\s\S]*?\$\$/g, ' (equation) ')
    .replace(/^\|.*\|$/gm, '')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/(\*\*|__|\*|_|~~)(.+?)\1/g, '$2')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/https?:\/\/\S+/g, '')
    .replace(/\n{2,}/g, '\n')
    .trim()
}

export function formatBytes(n: number) {
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 ** 2).toFixed(1)} MB`
}

export function relativeDay(ts: number): string {
  const d = new Date(ts * 1000)
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const diff = (today.getTime() - new Date(d).setHours(0, 0, 0, 0)) / 86_400_000
  if (diff <= 0) return 'Today'
  if (diff === 1) return 'Yesterday'
  if (diff < 7) return 'Previous 7 days'
  if (diff < 30) return 'Previous 30 days'
  return d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
}

export const ARTIFACT_LANGS = new Set(['html', 'svg', 'mermaid', 'markdown', 'md'])
