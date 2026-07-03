import { useCallback, useEffect, useRef, useState } from "react"
import { Link, useNavigate, useParams } from "react-router-dom"
import {
  ArrowLeft,
  Check,
  ChevronDown,
  ExternalLink,
  GitBranch,
  GitPullRequest,
  ListChecks,
  PenLine,
  SendHorizonal,
  X,
} from "lucide-react"
import { toast } from "sonner"

import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import { api } from "@/lib/api"
import { subscribeSse } from "@/lib/sse"
import { ago, clock, kTokens, money } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { Task, TaskEvent, Todo } from "@/lib/types"

/** Per-agent accent for the trace rail; the one place the UI spends color. */
const AGENT_COLOR: Record<string, string> = {
  orchestrator: "border-l-foreground/50 text-foreground",
  explorer: "border-l-sky-400 text-sky-600 dark:text-sky-400",
  coder: "border-l-emerald-400 text-emerald-600 dark:text-emerald-400",
  tester: "border-l-amber-400 text-amber-600 dark:text-amber-400",
  reviewer: "border-l-violet-400 text-violet-600 dark:text-violet-400",
}
const agentColor = (a: string) =>
  AGENT_COLOR[a] ?? "border-l-muted-foreground/40 text-muted-foreground"

function ToolOutput({ summary, text }: { summary: React.ReactNode; text: string }) {
  const [open, setOpen] = useState(false)
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
        <ChevronDown className={cn("size-3 transition-transform", !open && "-rotate-90")} />
        {summary}
      </CollapsibleTrigger>
      <CollapsibleContent>
        <pre className="mt-1 max-h-96 overflow-auto rounded-md bg-muted p-3 font-mono text-xs whitespace-pre-wrap">
          {text}
        </pre>
      </CollapsibleContent>
    </Collapsible>
  )
}

function EventBody({ ev }: { ev: TaskEvent }) {
  const c = ev.content ?? {}
  switch (ev.kind) {
    case "message":
    case "log":
      return <div className="text-sm whitespace-pre-wrap">{c.text}</div>
    case "status":
      return (
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <StatusBadge status={c.status} />
            {c.error && <span className="text-red-600 dark:text-red-400">{c.error}</span>}
            {c.usage?.cost_usd != null && (
              <span className="font-mono text-xs text-muted-foreground">
                {money(c.usage.cost_usd)} · {kTokens(c.usage.input_tokens, c.usage.output_tokens)}
              </span>
            )}
            {c.pr_url && (
              <a
                href={c.pr_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-xs underline underline-offset-2"
              >
                PR <ExternalLink className="size-3" />
              </a>
            )}
          </div>
          {c.summary && <ToolOutput summary="final summary" text={c.summary} />}
        </div>
      )
    case "tool_call":
      return (
        <div className="font-mono text-xs">
          <span className="text-muted-foreground">$</span>{" "}
          <span className="font-semibold">{c.name}</span>
          <span className="text-muted-foreground">
            ({JSON.stringify(c.args ?? {}).slice(1, 400)})
          </span>
        </div>
      )
    case "tool_result":
      return (
        <ToolOutput
          summary={
            <>
              {c.name || "result"}
              {c.truncated ? " (truncated)" : ""}
            </>
          }
          text={c.output ?? ""}
        />
      )
    case "pr_opened":
      return (
        <a
          href={c.url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-sm underline underline-offset-2"
        >
          <GitPullRequest className="size-3.5" /> Pull request opened
        </a>
      )
    case "approval_request":
      return (
        <div className="space-y-1">
          <div className="text-sm text-violet-600 dark:text-violet-400">
            Approval requested: {c.title ?? ""}
          </div>
          {c.diff_stat && <ToolOutput summary="diff stat" text={c.diff_stat} />}
        </div>
      )
    default:
      return <div className="font-mono text-xs text-muted-foreground">{JSON.stringify(c)}</div>
  }
}

function TodoPanel({ todos }: { todos: Todo[] }) {
  if (!todos.length) return null
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm">
          <ListChecks className="size-4" /> Plan
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-1">
        {todos.map((t, i) => (
          <div
            key={i}
            className={cn(
              "flex items-start gap-2 text-sm",
              t.status === "completed" && "text-muted-foreground line-through",
            )}
          >
            <span
              className={cn(
                "mt-1 size-2 shrink-0 rounded-full border",
                t.status === "completed"
                  ? "border-emerald-500 bg-emerald-500"
                  : t.status === "in_progress"
                    ? "border-amber-500 bg-amber-500 animate-pulse"
                    : "border-muted-foreground/40",
              )}
            />
            {t.content ?? t.title ?? ""}
          </div>
        ))}
      </CardContent>
    </Card>
  )
}

export default function TaskDetailPage() {
  const { id = "" } = useParams()
  const navigate = useNavigate()
  const [task, setTask] = useState<Task | null>(null)
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [todos, setTodos] = useState<Todo[]>([])
  const [approval, setApproval] = useState<Record<string, any> | null>(null)
  const [steer, setSteer] = useState("")
  const [reviseOpen, setReviseOpen] = useState(false)
  const [reviseText, setReviseText] = useState("")
  const [approving, setApproving] = useState(false)
  const lastIdRef = useRef(0)
  const bottomRef = useRef<HTMLDivElement | null>(null)

  const nearBottom = () =>
    window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 160

  const absorb = useCallback((ev: TaskEvent, live: boolean) => {
    if (ev.id <= lastIdRef.current) return
    lastIdRef.current = ev.id
    if (ev.kind === "todos") {
      setTodos(ev.content?.todos ?? [])
      return
    }
    const pinned = nearBottom()
    setEvents((prev) => [...prev, ev])
    if (ev.kind === "status") {
      const c = ev.content ?? {}
      setTask((prev) =>
        prev
          ? {
              ...prev,
              status: c.status ?? prev.status,
              pr_url: c.pr_url ?? prev.pr_url,
              error: c.error ?? prev.error,
              cost_usd: c.usage?.cost_usd ?? prev.cost_usd,
            }
          : prev,
      )
      if (c.status === "awaiting_approval") {
        api<TaskEvent[]>(`/api/tasks/${ev.task_id}/events`)
          .then((evs) => {
            const prop = [...evs].reverse().find((e) => e.kind === "approval_request")
            setApproval(prop?.content ?? {})
          })
          .catch(() => setApproval({}))
      }
    }
    if (ev.kind === "approval_request") setApproval(ev.content ?? {})
    if (live && pinned)
      requestAnimationFrame(() =>
        bottomRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }),
      )
  }, [])

  useEffect(() => {
    let stream: { close: () => void } | null = null
    let gone = false
    lastIdRef.current = 0
    setEvents([])
    setTodos([])
    setApproval(null)
    ;(async () => {
      try {
        const t = await api<Task>(`/api/tasks/${id}`)
        if (gone) return
        setTask(t)
        const history = await api<TaskEvent[]>(`/api/tasks/${id}/events`)
        if (gone) return
        history.forEach((ev) => absorb(ev, false))
        if (t.status === "awaiting_approval" && !history.some((e) => e.kind === "approval_request"))
          setApproval({})
        stream = subscribeSse(
          `/api/tasks/${id}/stream?after=${lastIdRef.current}`,
          (data) => absorb(JSON.parse(data), true),
        )
      } catch (e: any) {
        toast.error(e.message)
      }
    })()
    return () => {
      gone = true
      stream?.close()
    }
  }, [id, absorb])

  if (!task) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }

  const act = (path: string, ok: string) => async () => {
    try {
      await api(`/api/tasks/${id}/${path}`, { method: "POST" })
      toast.success(ok)
    } catch (e: any) {
      toast.error(e.message)
    }
  }

  const sendSteer = async (e: React.FormEvent) => {
    e.preventDefault()
    const msg = steer.trim()
    if (!msg) return
    setSteer("")
    try {
      await api(`/api/tasks/${id}/steer`, { method: "POST", body: JSON.stringify({ message: msg }) })
      toast.success("Message queued: the agent reads it at its next check")
    } catch (e: any) {
      toast.error(e.message)
    }
  }

  const queueRevision = async () => {
    const desc = reviseText.trim()
    if (!desc) return toast.error("Describe the revision first")
    try {
      const rt = await api<Task>("/api/tasks", {
        method: "POST",
        body: JSON.stringify({
          title: `Revise: ${task.title}`.slice(0, 200),
          description: desc,
          parent_id: task.id,
        }),
      })
      toast.success(`Revision ${rt.id} queued`)
      navigate(`/tasks/${rt.id}`)
    } catch (e: any) {
      toast.error(e.message)
    }
  }

  const approve = async () => {
    setApproving(true)
    try {
      const r = await api<{ pr_url?: string }>(`/api/tasks/${id}/approve`, { method: "POST" })
      toast.success(r.pr_url ? `Shipped: ${r.pr_url}` : "Shipped (branch pushed)")
      setApproval(null)
      const t = await api<Task>(`/api/tasks/${id}`)
      setTask(t)
    } catch (e: any) {
      toast.error(e.message)
    } finally {
      setApproving(false)
    }
  }

  const reject = async () => {
    try {
      await api(`/api/tasks/${id}/reject`, { method: "POST" })
      setApproval(null)
      const t = await api<Task>(`/api/tasks/${id}`)
      setTask(t)
    } catch (e: any) {
      toast.error(e.message)
    }
  }

  const cancellable = ["queued", "running", "awaiting_approval"].includes(task.status)
  const meta: Array<[string, React.ReactNode]> = [
    ["Repo", task.repo ?? "auto"],
    ["Source", task.source],
    ["Created", ago(task.created_at)],
    ["Cost", <span className="font-mono">{money(task.cost_usd)}</span>],
  ]
  if (task.model) meta.push(["Model", <span className="font-mono">{task.model}</span>])
  if (task.budget_usd) meta.push(["Budget", <span className="font-mono">{money(task.budget_usd, 2)}</span>])

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button asChild variant="ghost" size="sm">
          <Link to="/">
            <ArrowLeft className="size-4" /> Tasks
          </Link>
        </Button>
        <h1 className="font-mono text-lg font-semibold">{task.id}</h1>
        <span className="ml-auto flex gap-2">
          {task.status === "completed" && task.branch && (
            <Button variant="outline" size="sm" onClick={() => setReviseOpen((v) => !v)}>
              <PenLine className="size-4" /> Revise
            </Button>
          )}
          {cancellable && (
            <Button variant="destructive" size="sm" onClick={act("cancel", "Cancel requested")}>
              <X className="size-4" /> Cancel
            </Button>
          )}
        </span>
      </div>

      {approval && task.status === "awaiting_approval" && (
        <Card className="border-violet-300 dark:border-violet-800">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-violet-700 dark:text-violet-300">
              Awaiting your approval. Nothing has been pushed.
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="text-sm font-medium">{approval.title ?? task.title}</div>
            {approval.diff_stat && (
              <pre className="overflow-x-auto rounded-md bg-muted p-3 font-mono text-xs">
                {approval.diff_stat}
              </pre>
            )}
            {approval.summary && <ToolOutput summary="agent summary" text={approval.summary} />}
            <div className="flex gap-2">
              <Button onClick={approve} disabled={approving}>
                <Check className="size-4" /> Approve and ship
              </Button>
              <Button variant="destructive" onClick={reject}>
                <X className="size-4" /> Reject
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {reviseOpen && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Revise this task</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">
              A revision continues on the same branch and pull request.
            </p>
            <Textarea
              rows={4}
              placeholder="Review feedback: rename the helper, handle the empty-list case, add a test for..."
              value={reviseText}
              onChange={(e) => setReviseText(e.target.value)}
            />
            <Button onClick={queueRevision}>Queue revision</Button>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent className="space-y-3 pt-6">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-base font-semibold">{task.title}</span>
            <StatusBadge status={task.status} />
          </div>
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
            {meta.map(([k, v]) => (
              <span key={k as string} className="text-muted-foreground">
                {k} <span className="ml-1 text-foreground">{v}</span>
              </span>
            ))}
            {task.parent_id && (
              <span className="text-muted-foreground">
                Revises{" "}
                <Link to={`/tasks/${task.parent_id}`} className="font-mono underline underline-offset-2">
                  {task.parent_id}
                </Link>
              </span>
            )}
            {task.branch && (
              <span className="inline-flex items-center gap-1 font-mono text-muted-foreground">
                <GitBranch className="size-3.5" /> {task.branch}
              </span>
            )}
          </div>
          <p className="text-sm whitespace-pre-wrap text-muted-foreground">{task.description}</p>
          {task.pr_url && (
            <a
              href={task.pr_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-sm font-medium underline underline-offset-2"
            >
              <GitPullRequest className="size-4" /> View pull request
              <ExternalLink className="size-3" />
            </a>
          )}
          {task.error && (
            <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
              {task.error}
            </div>
          )}
        </CardContent>
      </Card>

      <TodoPanel todos={todos} />

      {task.status === "running" && (
        <form onSubmit={sendSteer} className="flex gap-2">
          <Input
            placeholder="Steer the running agent, e.g. 'also handle the empty-list case'"
            value={steer}
            onChange={(e) => setSteer(e.target.value)}
          />
          <Button type="submit" variant="secondary">
            <SendHorizonal className="size-4" /> Send
          </Button>
        </form>
      )}

      <div>
        <div className="mb-2 text-sm font-medium text-muted-foreground">
          Agent trace{task.status === "running" ? " · live" : ""}
        </div>
        <div className="space-y-px">
          {events.map((ev) => (
            <div
              key={ev.id}
              className={cn("flex gap-3 border-l-2 py-1.5 pl-3", agentColor(ev.agent))}
            >
              <div className="w-24 shrink-0 pt-0.5 text-right">
                <div className="truncate font-mono text-xs font-medium">{ev.agent}</div>
                <div className="font-mono text-[10px] text-muted-foreground">{clock(ev.ts)}</div>
              </div>
              <div className="min-w-0 flex-1 text-foreground">
                <EventBody ev={ev} />
              </div>
            </div>
          ))}
          {events.length === 0 && (
            <div className="py-8 text-center text-sm text-muted-foreground">
              No trace yet. Events appear here as the agent works.
            </div>
          )}
        </div>
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
