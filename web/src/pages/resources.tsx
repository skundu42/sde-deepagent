import { useCallback, useEffect, useState } from "react"
import { BookOpen, RefreshCw, Trash2 } from "lucide-react"
import { toast } from "sonner"

import { Empty } from "@/components/empty"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import { api } from "@/lib/api"
import { cn } from "@/lib/utils"
import type { RepoConfig, Resource } from "@/lib/types"

const STATUS_DOT: Record<string, string> = {
  done: "bg-emerald-500",
  queued: "bg-sky-500",
  extracting: "bg-amber-500",
  processing: "bg-amber-500",
  failed: "bg-red-500",
}

export default function ResourcesPage() {
  const [repos, setRepos] = useState<Record<string, RepoConfig>>({})
  const [docs, setDocs] = useState<Resource[] | null>(null)
  const [content, setContent] = useState("")
  const [scope, setScope] = useState("global")
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api<Resource[]>("/api/resources")
      .then(setDocs)
      .catch((e) => {
        setDocs([])
        toast.error(e.message)
      })
  }, [])

  useEffect(() => {
    api<Record<string, RepoConfig>>("/api/repos").then(setRepos).catch(() => {})
    load()
  }, [load])

  const ingest = async () => {
    const text = content.trim()
    if (!text) return toast.error("Paste a URL or some text first")
    setBusy(true)
    try {
      await api("/api/resources", {
        method: "POST",
        body: JSON.stringify({ content: text, scope }),
      })
      toast.success("Ingesting. Indexing can take a moment.")
      setContent("")
      setTimeout(load, 800)
    } catch (e: any) {
      toast.error(e.message)
    } finally {
      setBusy(false)
    }
  }

  const remove = async (id: string) => {
    try {
      await api(`/api/resources/${id}`, { method: "DELETE" })
      load()
    } catch (e: any) {
      toast.error(e.message)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">Resources</h1>
        <span className="text-sm text-muted-foreground">
          Links and docs the agents can recall
        </span>
        <Button variant="outline" size="sm" className="ml-auto" onClick={load}>
          <RefreshCw className="size-4" /> Refresh
        </Button>
      </div>

      <Card className="max-w-2xl">
        <CardHeader>
          <CardTitle>Add to company memory</CardTitle>
          <CardDescription>
            URLs are fetched, extracted and indexed; agents and chat can then
            recall the content. GitHub URLs also let chat read the repo source.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="res-content">URL or text</Label>
            <Textarea
              id="res-content"
              rows={3}
              placeholder="https://docs.yourcompany.com/architecture, or paste any text: API conventions, runbooks, onboarding notes"
              value={content}
              onChange={(e) => setContent(e.target.value)}
            />
          </div>
          <div className="max-w-64 space-y-2">
            <Label>Scope</Label>
            <Select value={scope} onValueChange={setScope}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="global">Global (all codebases)</SelectItem>
                {Object.keys(repos).map((n) => (
                  <SelectItem key={n} value={n}>
                    Repo: {n}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button onClick={ingest} disabled={busy}>
            Ingest
          </Button>
        </CardContent>
      </Card>

      <div className="text-sm font-medium text-muted-foreground">Ingested resources</div>
      {docs === null ? (
        <div className="space-y-2">
          {[...Array(3)].map((_, i) => (
            <Skeleton key={i} className="h-14 w-full" />
          ))}
        </div>
      ) : docs.length === 0 ? (
        <Empty icon={BookOpen} title="No resources yet">
          Paste a docs link or some text above to give your agents company context.
        </Empty>
      ) : (
        <div className="overflow-hidden rounded-lg border">
          {docs.map((d) => (
            <div
              key={d.id}
              className="flex items-center gap-3 border-b px-4 py-3 last:border-b-0"
            >
              <span
                className={cn(
                  "size-2 shrink-0 rounded-full",
                  STATUS_DOT[d.status ?? ""] ?? "bg-muted-foreground/40",
                )}
                title={d.status}
              />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium">
                  {d.title || d.summary || d.id}
                </div>
                {d.summary && d.title && (
                  <div className="truncate text-xs text-muted-foreground">{d.summary}</div>
                )}
              </div>
              <Badge variant="outline">{d.kind}</Badge>
              <Badge variant={d.scope === "global" ? "secondary" : "default"}>{d.scope}</Badge>
              <span className="w-16 text-xs text-muted-foreground">{d.status ?? "?"}</span>
              <Button
                variant="ghost"
                size="icon"
                aria-label="Delete resource"
                onClick={() => remove(d.id)}
              >
                <Trash2 className="size-4" />
              </Button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
