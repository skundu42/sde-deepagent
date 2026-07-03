/** Server-sent events over fetch() instead of EventSource, so the bearer
 * token travels in the Authorization header. EventSource can only put it in
 * the URL, and query strings end up in server and reverse-proxy access logs;
 * this token is control-plane access. */

import { auth } from "@/lib/api"

const RETRY_MS = 3000

export interface SseHandle {
  close: () => void
}

/** Split a raw SSE buffer into complete event payloads (their data: lines
 * joined) and the unconsumed remainder. Exported for tests. */
export function parseSseBuffer(buf: string): { events: string[]; rest: string } {
  const parts = buf.split(/\r?\n\r?\n/)
  const rest = parts.pop() ?? ""
  const events: string[] = []
  for (const part of parts) {
    const data = part
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""))
      .join("\n")
    if (data) events.push(data)
  }
  return { events, rest }
}

/** Subscribe to an SSE endpoint; reconnects on drop like EventSource does.
 * Returns a handle whose close() ends the subscription for good. */
export function subscribeSse(
  url: string,
  onMessage: (data: string) => void,
  opts: { onOpen?: () => void; onError?: () => void } = {},
): SseHandle {
  let closed = false
  let ctrl = new AbortController()

  const run = async () => {
    while (!closed) {
      ctrl = new AbortController()
      try {
        const headers: Record<string, string> = { Accept: "text/event-stream" }
        const token = auth.token
        if (token) headers.Authorization = `Bearer ${token}`
        const res = await fetch(url, {
          headers,
          signal: ctrl.signal,
          cache: "no-store",
        })
        if (!res.ok || !res.body) throw new Error(`stream returned ${res.status}`)
        opts.onOpen?.()
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buf = ""
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          buf += decoder.decode(value, { stream: true })
          const { events, rest } = parseSseBuffer(buf)
          buf = rest
          for (const data of events) {
            if (closed) return
            onMessage(data)
          }
        }
        // server closed the stream: fall through to reconnect
        throw new Error("stream ended")
      } catch {
        if (closed) return
        opts.onError?.()
        await new Promise((resolve) => setTimeout(resolve, RETRY_MS))
      }
    }
  }
  void run()

  return {
    close: () => {
      closed = true
      ctrl.abort()
    },
  }
}
