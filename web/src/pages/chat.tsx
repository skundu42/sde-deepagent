import { useEffect, useRef, useState } from "react"
import { Bot, RotateCcw, SendHorizonal, User } from "lucide-react"
import { toast } from "sonner"

import { Markdown } from "@/components/markdown"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { api } from "@/lib/api"
import { money } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { ChatReply } from "@/lib/types"

interface Msg {
  role: "user" | "assistant"
  text: string
  cost?: number
}

const GREETING: Msg = {
  role: "assistant",
  text:
    "Ask me about any task, codebase or ingested resource: what was done, why " +
    "a run failed, what it cost, or how something in the code works.",
}

// module-level so the conversation survives route changes within the session
const chatState: { sessionId: string | null; msgs: Msg[] } = {
  sessionId: sessionStorage.getItem("chat_session"),
  msgs: [],
}

export default function ChatPage() {
  const [msgs, setMsgs] = useState<Msg[]>(chatState.msgs.length ? chatState.msgs : [GREETING])
  const [input, setInput] = useState("")
  const [waiting, setWaiting] = useState(false)
  const endRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    chatState.msgs = msgs
  }, [msgs])
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" })
  }, [msgs, waiting])

  const reset = async () => {
    if (chatState.sessionId) {
      try {
        await api(`/api/chat/${chatState.sessionId}`, { method: "DELETE" })
      } catch {
        /* the session may already be gone server-side */
      }
    }
    chatState.sessionId = null
    sessionStorage.removeItem("chat_session")
    setMsgs([GREETING])
  }

  const send = async (e: React.FormEvent) => {
    e.preventDefault()
    const text = input.trim()
    if (!text || waiting) return
    setInput("")
    setMsgs((m) => [...m, { role: "user", text }])
    setWaiting(true)
    try {
      const res = await api<ChatReply>("/api/chat", {
        method: "POST",
        body: JSON.stringify({ message: text, session_id: chatState.sessionId }),
      })
      chatState.sessionId = res.session_id
      sessionStorage.setItem("chat_session", res.session_id)
      setMsgs((m) => [...m, { role: "assistant", text: res.reply, cost: res.cost_usd }])
    } catch (err: any) {
      toast.error(err.message)
      setMsgs((m) => [...m, { role: "assistant", text: `Something went wrong: ${err.message}` }])
    } finally {
      setWaiting(false)
    }
  }

  return (
    <div className="flex h-[calc(100svh-6rem)] flex-col space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">Chat</h1>
        <span className="text-sm text-muted-foreground">
          Ask about any past or running task
        </span>
        <Button variant="outline" size="sm" className="ml-auto" onClick={reset}>
          <RotateCcw className="size-4" /> New conversation
        </Button>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto rounded-lg border p-4">
        {msgs.map((m, i) => (
          <div key={i} className={cn("flex gap-3", m.role === "user" && "justify-end")}>
            {m.role === "assistant" && (
              <div className="flex size-7 shrink-0 items-center justify-center rounded-full bg-muted">
                <Bot className="size-4" />
              </div>
            )}
            <div
              className={cn(
                "max-w-[85%] rounded-lg px-3 py-2",
                m.role === "user" ? "bg-primary text-primary-foreground" : "bg-muted/50",
              )}
            >
              {m.role === "user" ? (
                <div className="text-sm whitespace-pre-wrap">{m.text}</div>
              ) : (
                <Markdown text={m.text} />
              )}
              {m.cost != null && (
                <div className="mt-1 font-mono text-[10px] text-muted-foreground">
                  {money(m.cost)}
                </div>
              )}
            </div>
            {m.role === "user" && (
              <div className="flex size-7 shrink-0 items-center justify-center rounded-full bg-muted">
                <User className="size-4" />
              </div>
            )}
          </div>
        ))}
        {waiting && (
          <div className="flex gap-3">
            <div className="flex size-7 shrink-0 items-center justify-center rounded-full bg-muted">
              <Bot className="size-4" />
            </div>
            <div className="rounded-lg bg-muted/50 px-3 py-2 text-sm text-muted-foreground">
              Consulting tasks, code and memory
              <span className="animate-pulse">...</span>
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form onSubmit={send} className="flex gap-2">
        <Input
          autoFocus
          placeholder='e.g. "what did the agent change in the subtract task?"'
          value={input}
          onChange={(e) => setInput(e.target.value)}
        />
        <Button type="submit" disabled={waiting || !input.trim()}>
          <SendHorizonal className="size-4" /> Send
        </Button>
      </form>
    </div>
  )
}
