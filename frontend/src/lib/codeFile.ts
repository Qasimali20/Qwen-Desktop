/** Choose a sensible file name for a code block the user wants to download. */

const EXT: Record<string, string> = {
  python: 'py', py: 'py', javascript: 'js', js: 'js', jsx: 'jsx', typescript: 'ts', ts: 'ts', tsx: 'tsx',
  html: 'html', css: 'css', scss: 'scss', json: 'json', yaml: 'yaml', yml: 'yml', toml: 'toml', xml: 'xml',
  bash: 'sh', sh: 'sh', shell: 'sh', zsh: 'sh', powershell: 'ps1', ps1: 'ps1', sql: 'sql', go: 'go', rust: 'rs',
  rs: 'rs', java: 'java', kotlin: 'kt', kt: 'kt', swift: 'swift', c: 'c', cpp: 'cpp', 'c++': 'cpp', csharp: 'cs',
  cs: 'cs', php: 'php', ruby: 'rb', rb: 'rb', r: 'r', lua: 'lua', dart: 'dart', scala: 'scala', vue: 'vue',
  svelte: 'svelte', markdown: 'md', md: 'md', svg: 'svg', mermaid: 'mmd', dockerfile: 'Dockerfile',
  makefile: 'Makefile', ini: 'ini', graphql: 'graphql', proto: 'proto', hcl: 'tf', terraform: 'tf', text: 'txt',
  plaintext: 'txt', csv: 'csv', diff: 'patch', patch: 'patch',
}

const FILE_RE = /^[\w@][\w@.\-/]*\.[\w]{1,12}$|^(Dockerfile|Makefile|Procfile|Jenkinsfile)$/

function clean(candidate: string | undefined): string | null {
  if (!candidate) return null
  const c = candidate.trim().replace(/^["'`]|["'`]$/g, '').replace(/^\.?\//, '')
  return FILE_RE.test(c) && !c.includes('..') ? c : null
}

/** From the fence info after the language: ```python app/main.py  or  ```ts title="src/x.ts". */
function fromMeta(meta: string | undefined): string | null {
  if (!meta) return null
  const kv = /(?:title|file|filename|path)\s*=\s*("[^"]+"|'[^']+'|\S+)/.exec(meta)
  return clean(kv?.[1]) ?? clean(meta.split(/\s+/).find((t) => clean(t)))
}

/** From a first-line comment that is just a path: "# app/main.py", "// file: src/x.ts", "<!-- index.html -->". */
function fromFirstLine(code: string): string | null {
  const first = code.split('\n', 1)[0].trim()
  const m = /^(?:#|\/\/|--|;|<!--|\/\*)\s*(?:file(?:name)?:\s*)?([^\s*>]+)\s*(?:-->|\*\/)?$/.exec(first)
  return clean(m?.[1])
}

export function isPatch(lang: string, code: string): boolean {
  return lang === 'diff' || lang === 'patch' || /^(---|diff --git) .+\n(\+\+\+ |index )/m.test(code)
}

export function codeFileName(lang: string, code: string, meta?: string): string {
  if (isPatch(lang, code)) {
    const target = /^\+\+\+ (?:b\/)?(\S+)/m.exec(code)?.[1]
    const base = target && target !== '/dev/null' ? target.split('/').pop()!.replace(/\.[^.]+$/, '') : 'changes'
    return `${base}.patch`
  }
  const named = fromMeta(meta) ?? fromFirstLine(code)
  if (named) return named.split('/').pop()!
  const ext = EXT[lang] ?? (lang && /^\w{1,8}$/.test(lang) ? lang : 'txt')
  return ext === 'Dockerfile' || ext === 'Makefile' ? ext : `snippet.${ext}`
}

export function downloadText(filename: string, text: string): void {
  const blob = new Blob([text.endsWith('\n') ? text : `${text}\n`], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = Object.assign(document.createElement('a'), { href: url, download: filename })
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
