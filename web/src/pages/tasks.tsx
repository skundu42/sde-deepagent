import { useEffect, useMemo, useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import { ExternalLink, GitBranch, Inbox, Plus } from "lucide-react"
import { toast } from "sonner"

import { taskEvents } from "@/components/layout"
import { Empty } from "@/components/empty"
import { StatusBadge, STATUS_STYLES } from "@/components/status-badge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { api } from "@/lib/api"
import { ago } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { Task, TaskStatus } from "@/lib/types"

const FILTER_ORDER: TaskStatus[] = [
  "running",
  "queued",
  "awaiting_approval",
  "completed",
  "failed",
  "cancelled",
]

export default function TasksPage() {
  const [tasks, setTasks] = useState<Task[] | null>(null)
  const [filter, setFilter] = useState<Set<TaskStatus>>(new Set())
  const navigate = useNavigate()

  const load = () =>
    api<Task[]>("/api/tasks")
      .then(setTasks)
      .catch((e) => toast.error(e.message))

  useEffect(() => {
    load()
    const onStatus = () => load()
    taskEvents.addEventListener("task-status", onStatus)
    return () => taskEvents.removeEventListener("task-status", onStatus)
  }, [])

  const counts = useMemo(() => {
    const c = {} as Record<TaskStatus, number>
    for (const t of tasks ?? []) c[t.status] = (c[t.status] ?? 0) + 1
    return c
  }, [tasks])

  if (tasks === null) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-8 w-40" />
        {[...Array(4)].map((_, i) => (
          <Skeleton key={i} className="h-16 w-full" />
        ))}
      </div>
    )
  }

  const active = filter.size > 0
  const shown = active ? tasks.filter((t) => filter.has(t.status)) : tasks
  const toggle = (s: TaskStatus | "all") =>
    setFilter((prev) => {
      if (s === "all") return new Set()
      const next = new Set(prev)
      if (next.has(s)) next.delete(s)
      else next.add(s)
      return next
    })

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">Tasks</h1>
        <span className="text-sm text-muted-foreground">
          {active ? `${shown.length} of ${tasks.length}` : `${tasks.length} total`}
        </span>
        <Button asChild size="sm" className="ml-auto">
          <Link to="/new">
            <Plus className="size-4" /> New task
          </Link>
        </Button>
      </div>

      {tasks.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          <button
            onClick={() => toggle("all")}
            className={cn(
              "rounded-full border px-2.5 py-0.5 text-xs transition-colors",
              !active
                ? "border-foreground/30 bg-accent font-medium"
                : "text-muted-foreground hover:bg-accent",
            )}
          >
            all <span className="opacity-60">{tasks.length}</span>
          </button>
          {FILTER_ORDER.map((s) => (
            <button
              key={s}
              onClick={() => toggle(s)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs transition-colors",
                filter.has(s)
                  ? "border-foreground/30 bg-accent font-medium"
                  : "text-muted-foreground hover:bg-accent",
                !counts[s] && "opacity-40",
              )}
            >
              <span className={cn("size-1.5 rounded-full", STATUS_STYLES[s].dot)} />
              {STATUS_STYLES[s].label} <span className="opacity-60">{counts[s] ?? 0}</span>
            </button>
          ))}
        </div>
      )}

      {tasks.length === 0 ? (
        <Empty icon={Inbox} title="No tasks yet">
          Create one here, or send a message through Telegram, Slack or Linear.
        </Empty>
      ) : shown.length === 0 ? (
        <Empty icon={Inbox} title="No matching tasks">
          Nothing matches the selected status filter.
        </Empty>
      ) : (
        <div className="overflow-hidden rounded-lg border">
          {shown.map((t) => (
            <button
              key={t.id}
              onClick={() => navigate(`/tasks/${t.id}`)}
              className="grid w-full grid-cols-[1fr_auto] items-center gap-x-4 gap-y-1 border-b px-4 py-3 text-left transition-colors last:border-b-0 hover:bg-accent/50"
            >
              <div className="flex min-w-0 items-center gap-2.5">
                <span className="shrink-0 font-mono text-xs text-muted-foreground">{t.id}</span>
                <span className="truncate text-sm font-medium">{t.title}</span>
              </div>
              <div className="flex items-center gap-2">
                <StatusBadge status={t.status} />
                <span className="hidden w-20 text-right text-xs text-muted-foreground sm:inline">
                  {ago(t.created_at)}
                </span>
              </div>
              <div className="col-span-2 flex min-w-0 flex-wrap items-center gap-1.5">
                {t.repo && <Badge variant="secondary">{t.repo}</Badge>}
                <Badge variant="outline" className="text-muted-foreground">
                  {t.source}
                </Badge>
                {t.branch && (
                  <span className="inline-flex min-w-0 items-center gap-1 font-mono text-xs text-muted-foreground">
                    <GitBranch className="size-3 shrink-0" />
                    <span className="truncate">{t.branch}</span>
                  </span>
                )}
                {t.pr_url && (
                  <a
                    href={t.pr_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={(e) => e.stopPropagation()}
                    className="inline-flex items-center gap-1 text-xs font-medium text-foreground underline underline-offset-2"
                  >
                    PR <ExternalLink className="size-3" />
                  </a>
                )}
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
