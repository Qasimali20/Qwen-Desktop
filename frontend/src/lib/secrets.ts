/**
 * Detects credentials in text before it is sent, so the composer can offer to redact them.
 * Mirrors backend/app/secretscan.py — keep the two rule lists in step.
 */

interface Rule {
  kind: string
  label: string
  re: RegExp
  group?: number // capture group holding the secret (default: whole match)
  keep?: number // leading characters kept as a hint, e.g. "ghp_"
  check?: boolean // value must pass looksReal (assignment-style rules)
}

const RULES: Rule[] = [
  { kind: 'private_key', label: 'Private key', re: /-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|$)/g },
  { kind: 'aws_access_key', label: 'AWS access key', re: /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g, keep: 4 },
  { kind: 'github_token', label: 'GitHub token', re: /\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{60,255})\b/g, keep: 4 },
  { kind: 'gitlab_token', label: 'GitLab token', re: /\bglpat-[A-Za-z0-9_-]{20,}/g, keep: 6 },
  { kind: 'anthropic_key', label: 'Anthropic API key', re: /\bsk-ant-[A-Za-z0-9_-]{20,}/g, keep: 7 },
  { kind: 'openai_key', label: 'OpenAI-style API key', re: /\bsk-(?!ant-)(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}/g, keep: 3 },
  { kind: 'qwen_desktop_key', label: 'Qwen Desktop API key', re: /\bqd-[A-Za-z0-9_-]{40,}/g, keep: 3 },
  { kind: 'slack_token', label: 'Slack token', re: /\bxox[abposr]-[A-Za-z0-9-]{10,}/g, keep: 5 },
  { kind: 'slack_webhook', label: 'Slack webhook', re: /https:\/\/hooks\.slack\.com\/services\/[A-Za-z0-9/_-]{20,}/g },
  { kind: 'stripe_key', label: 'Stripe secret key', re: /\b(?:sk|rk)_live_[A-Za-z0-9]{20,}/g, keep: 8 },
  { kind: 'google_api_key', label: 'Google API key', re: /\bAIza[0-9A-Za-z_-]{35}\b/g, keep: 4 },
  { kind: 'hf_token', label: 'Hugging Face token', re: /\bhf_[A-Za-z0-9]{30,}\b/g, keep: 3 },
  { kind: 'jwt', label: 'JSON Web Token', re: /\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g, keep: 3 },
  { kind: 'url_password', label: 'Password in a URL', re: /\b[a-zA-Z][a-zA-Z0-9+.-]{1,20}:\/\/[^\s:/@'"]{1,64}:([^\s@/'"]{3,128})@/g, group: 1, check: true },
  {
    kind: 'assignment', label: 'Password or secret', group: 1, check: true,
    re: /(?:^|[^A-Za-z0-9_])(?:[A-Za-z0-9_]*(?:password|passwd|pwd|secret|api[_-]?key|apikey|access[_-]?token|auth[_-]?token|private[_-]?key|client[_-]?secret|bearer))["']?\s*(?:[:=]|=>|:=)\s*["']?([^\s"'`,;)\]}]{8,})/gi,
  },
  { kind: 'bearer_header', label: 'Authorization header', re: /\bAuthorization:\s*(?:Bearer|Basic|token)\s+([A-Za-z0-9_\-.=:/+]{16,})/gi, group: 1, check: true },
]

const PLACEHOLDER = /^(?:\$\{?|\{\{|<|%|process\.env|os\.environ|os\.getenv|env\(|getenv|settings\.|config\.|self\.|this\.|\*{3,}|x{4,}|\.{3}|your[_-]|my[_-]|example|changeme|placeholder|redacted|dummy|sample|secret$|password$|none$|null$|nil$|true$|false$|undefined$|string$|str$|bytes$|optional|required|todo)/i

function looksReal(value: string): boolean {
  if (PLACEHOLDER.test(value) || value.includes('(') || value.includes('[') || value.includes('REDACTED')) return false
  if (/^[a-z_]+(?:\.[a-z_]+)*$/.test(value)) return false
  return new Set(value).size >= 5
}

export interface SecretFinding {
  kind: string
  label: string
  start: number
  end: number
  line: number
  hint: string
}

/** Start/end of a capture group (needs the `d` flag's indices; falls back to searching inside the match). */
function groupSpan(m: RegExpExecArray, group: number): [number, number] | null {
  if (!group) return [m.index, m.index + m[0].length]
  const value = m[group]
  if (value == null) return null
  const ind = (m as RegExpExecArray & { indices?: [number, number][] }).indices?.[group]
  if (ind) return [ind[0], ind[1]]
  const off = m[0].lastIndexOf(value)
  return [m.index + off, m.index + off + value.length]
}

export function findSecrets(text: string, limit = 50): SecretFinding[] {
  if (!text) return []
  const taken: [number, number][] = []
  const out: SecretFinding[] = []
  for (const rule of RULES) {
    const re = new RegExp(rule.re.source, rule.re.flags.includes('d') ? rule.re.flags : `${rule.re.flags}d`)
    for (let m = re.exec(text); m; m = re.exec(text)) {
      if (m[0].length === 0) { re.lastIndex++; continue }
      const span = groupSpan(m, rule.group ?? 0)
      if (!span || span[1] <= span[0]) continue
      const [start, end] = span
      const value = text.slice(start, end)
      if (rule.check && !looksReal(value)) continue
      if (taken.some(([s, e]) => s < end && start < e)) continue
      taken.push([start, end])
      out.push({ kind: rule.kind, label: rule.label, start, end, line: text.slice(0, start).split('\n').length, hint: value.slice(0, rule.keep ?? 0) })
      if (out.length >= limit) break
    }
  }
  return out.sort((a, b) => a.start - b.start)
}

export function maskValue(f: SecretFinding): string {
  return f.hint ? `${f.hint}…[REDACTED ${f.kind}]` : `[REDACTED ${f.kind}]`
}

export function redactSecrets(text: string, findings = findSecrets(text, 10_000)): string {
  let out = ''
  let pos = 0
  for (const f of findings) {
    out += text.slice(pos, f.start) + maskValue(f)
    pos = f.end
  }
  return out + text.slice(pos)
}

/** "GitHub token, 2 × Password or secret" */
export function describeFindings(findings: SecretFinding[]): string {
  const counts = new Map<string, number>()
  for (const f of findings) counts.set(f.label, (counts.get(f.label) ?? 0) + 1)
  return [...counts].map(([label, n]) => (n > 1 ? `${n} × ${label}` : label)).join(', ')
}
