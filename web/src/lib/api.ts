/** REST + SSE client with bearer-token auth.
 *
 * The token lives in sessionStorage. A 401 fires the "unauthorized" listeners
 * (the layout opens the token dialog); when a token is saved, "changed"
 * listeners fire so open SSE streams reconnect and views re-fetch.
 */

const TOKEN_KEY = "auth_token"

type Listener = () => void
const listeners: Record<"unauthorized" | "changed", Set<Listener>> = {
  unauthorized: new Set(),
  changed: new Set(),
}

export const auth = {
  get token(): string {
    try {
      return sessionStorage.getItem(TOKEN_KEY) ?? ""
    } catch {
      return ""
    }
  },
  set(token: string) {
    try {
      sessionStorage.setItem(TOKEN_KEY, token.trim())
    } catch {
      /* private mode: keep going without persistence */
    }
    listeners.changed.forEach((fn) => fn())
  },
  on(event: "unauthorized" | "changed", fn: Listener): () => void {
    listeners[event].add(fn)
    return () => listeners[event].delete(fn)
  },
}

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

/** FastAPI error payloads: {detail: string | [{loc, msg}, ...] | object}. */
export function detailToMessage(detail: unknown, fallback: string): string {
  if (detail == null) return fallback
  if (typeof detail === "string") return detail
  if (Array.isArray(detail)) {
    return detail
      .map((d) => `${(d.loc ?? []).slice(1).join(".") || "request"}: ${d.msg}`)
      .join("; ")
  }
  return JSON.stringify(detail)
}

export async function api<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const token = auth.token
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((opts.headers as Record<string, string>) ?? {}),
  }
  if (token) headers.Authorization = `Bearer ${token}`
  const res = await fetch(path, { ...opts, headers })
  if (res.status === 401) {
    // a concurrent call may have just set a fresh token: retry with it once
    if (auth.token !== token) return api(path, opts)
    listeners.unauthorized.forEach((fn) => fn())
    throw new ApiError("This server requires an API token", 401)
  }
  if (!res.ok) {
    let detail: unknown = res.statusText
    try {
      detail = (await res.json()).detail ?? detail
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detailToMessage(detail, res.statusText), res.status)
  }
  return res.json()
}

// SSE subscriptions live in ./sse.ts: they stream via fetch() so the token
// travels in the Authorization header, never in a log-visible query string.
