import { cn } from "@/lib/utils"
import type { TaskStatus } from "@/lib/types"

/** One color per task state, used consistently across badges, rows and dots.
 * The chrome around them stays neutral so these carry the information. */
export const STATUS_STYLES: Record<TaskStatus, { badge: string; dot: string; label: string }> = {
  queued: {
    badge:
      "bg-sky-50 text-sky-700 border-sky-200 dark:bg-sky-950 dark:text-sky-300 dark:border-sky-900",
    dot: "bg-sky-500",
    label: "queued",
  },
  running: {
    badge:
      "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:border-amber-900",
    dot: "bg-amber-500",
    label: "running",
  },
  awaiting_approval: {
    badge:
      "bg-violet-50 text-violet-700 border-violet-200 dark:bg-violet-950 dark:text-violet-300 dark:border-violet-900",
    dot: "bg-violet-500",
    label: "needs approval",
  },
  completed: {
    badge:
      "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:border-emerald-900",
    dot: "bg-emerald-500",
    label: "completed",
  },
  failed: {
    badge:
      "bg-red-50 text-red-700 border-red-200 dark:bg-red-950 dark:text-red-300 dark:border-red-900",
    dot: "bg-red-500",
    label: "failed",
  },
  cancelled: {
    badge: "bg-muted text-muted-foreground border-border",
    dot: "bg-muted-foreground",
    label: "cancelled",
  },
}

export function StatusBadge({ status, className }: { status: TaskStatus; className?: string }) {
  const s = STATUS_STYLES[status] ?? STATUS_STYLES.queued
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap",
        s.badge,
        className,
      )}
    >
      <span className={cn("size-1.5 rounded-full", s.dot, status === "running" && "animate-pulse")} />
      {s.label}
    </span>
  )
}
