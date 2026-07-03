/** "42s ago" / "5m ago" / "3h ago", falling back to a local date for older. */
export function ago(ts: number | null | undefined, now = Date.now()): string {
  if (!ts) return "-"
  const s = Math.max(0, now / 1000 - ts)
  if (s < 60) return `${Math.floor(s)}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return new Date(ts * 1000).toLocaleString()
}

export function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false })
}

export function money(usd: number | null | undefined, digits = 4): string {
  if (usd == null) return "-"
  return `$${usd.toFixed(digits)}`
}

export function kTokens(input?: number | null, output?: number | null): string {
  const total = (input ?? 0) + (output ?? 0)
  return `${Math.round(total / 1000)}k tok`
}

/** "NAME=value" lines to an object; ignores blanks and lines without "=". */
export function parseKvLines(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const raw of text.split("\n")) {
    const line = raw.trim()
    if (!line) continue
    const i = line.indexOf("=")
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim()
  }
  return out
}

/** Secrets textarea: NAME=env:HOST_VAR stays a reference; NAME=value becomes a
 * "store" reference plus an encrypted-store value. */
export function parseSecretLines(text: string): {
  refs: Record<string, string>
  values: Record<string, string>
} {
  const refs: Record<string, string> = {}
  const values: Record<string, string> = {}
  for (const [name, val] of Object.entries(parseKvLines(text))) {
    if (val.startsWith("env:")) refs[name] = val
    else {
      refs[name] = "store"
      values[name] = val
    }
  }
  return { refs, values }
}
