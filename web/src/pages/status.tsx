import { useEffect, useState } from "react"
import { RefreshCw } from "lucide-react"
import { toast } from "sonner"

import { Skeleton } from "@/components/ui/skeleton"
import { Button } from "@/components/ui/button"
import { api } from "@/lib/api"
import { cn } from "@/lib/utils"
import type { StatusResponse } from "@/lib/types"

const STATE_DOT: Record<string, string> = {
  ok: "bg-emerald-500",
  warn: "bg-amber-500",
  down: "bg-red-500",
  off: "bg-muted-foreground/40",
  unconfigured: "bg-muted-foreground/40",
}
const STATE_WORD: Record<string, string> = {
  ok: "ok",
  warn: "warn",
  down: "down",
  off: "off",
  unconfigured: "not set",
}

export default function StatusPage() {
  const [data, setData] = useState<StatusResponse | null>(null)

  const load = () =>
    api<StatusResponse>("/api/status")
      .then(setData)
      .catch((e) => toast.error(e.message))

  useEffect(() => {
    load()
    const timer = setInterval(load, 10_000)
    return () => clearInterval(timer)
  }, [])

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">Status</h1>
        {data && (
          <span className="text-sm text-muted-foreground">
            Live health of every component · v{data.version}
          </span>
        )}
        <Button variant="outline" size="sm" className="ml-auto" onClick={load}>
          <RefreshCw className="size-4" /> Refresh
        </Button>
      </div>

      {!data ? (
        <div className="space-y-2">
          {[...Array(6)].map((_, i) => (
            <Skeleton key={i} className="h-12 w-full" />
          ))}
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border">
          {data.components.map((c) => (
            <div
              key={c.key}
              className="grid grid-cols-[auto_11rem_4rem_1fr] items-center gap-3 border-b px-4 py-3 last:border-b-0 max-sm:grid-cols-[auto_1fr]"
            >
              <span className={cn("size-2 rounded-full", STATE_DOT[c.state])} />
              <span className="text-sm font-medium">{c.label}</span>
              <span
                className={cn(
                  "text-xs font-medium uppercase max-sm:hidden",
                  c.state === "ok" && "text-emerald-600 dark:text-emerald-400",
                  c.state === "warn" && "text-amber-600 dark:text-amber-400",
                  c.state === "down" && "text-red-600 dark:text-red-400",
                  (c.state === "off" || c.state === "unconfigured") && "text-muted-foreground",
                )}
              >
                {STATE_WORD[c.state] ?? c.state}
              </span>
              <span className="text-sm text-muted-foreground max-sm:col-span-2">{c.detail}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
