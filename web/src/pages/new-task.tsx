import { useEffect, useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import { toast } from "sonner"

import { ModelSelect } from "@/components/model-select"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { api } from "@/lib/api"
import type { ModelCatalog, RepoConfig, Task } from "@/lib/types"

const AUTO = "__auto__"

export default function NewTaskPage() {
  const [repos, setRepos] = useState<Record<string, RepoConfig>>({})
  const [catalog, setCatalog] = useState<ModelCatalog>({})
  const [title, setTitle] = useState("")
  const [description, setDescription] = useState("")
  const [repo, setRepo] = useState<string>(AUTO)
  const [model, setModel] = useState<string | null>(null)
  const [budget, setBudget] = useState("")
  const [busy, setBusy] = useState(false)
  const navigate = useNavigate()

  useEffect(() => {
    api<Record<string, RepoConfig>>("/api/repos").then(setRepos).catch(() => {})
    api<ModelCatalog>("/api/models").then(setCatalog).catch(() => {})
  }, [])

  const submit = async () => {
    if (!title.trim()) return toast.error("A title is required")
    setBusy(true)
    try {
      const t = await api<Task>("/api/tasks", {
        method: "POST",
        body: JSON.stringify({
          title: title.trim(),
          description: description.trim() || title.trim(),
          repo: repo === AUTO ? null : repo,
          model,
          budget_usd: parseFloat(budget) || null,
        }),
      })
      toast.success(`Task ${t.id} queued`)
      navigate(`/tasks/${t.id}`)
    } catch (e: any) {
      toast.error(e.message)
      setBusy(false)
    }
  }

  const repoNames = Object.keys(repos)
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">New task</h1>
      <Card className="max-w-2xl">
        <CardHeader>
          <CardTitle>Dispatch work to the agent</CardTitle>
          <CardDescription>
            The orchestrator plans the task, delegates to subagents, runs tests and
            opens a pull request.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="t-title">Title</Label>
            <Input
              id="t-title"
              autoFocus
              placeholder="Fix flaky login test"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="t-desc">Description</Label>
            <Textarea
              id="t-desc"
              rows={5}
              placeholder="Full task details: what to change, acceptance criteria, links"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label>Codebase</Label>
              <Select value={repo} onValueChange={setRepo}>
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={AUTO}>Auto-detect from the task</SelectItem>
                  {repoNames.map((n) => (
                    <SelectItem key={n} value={n}>
                      {n}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {repoNames.length === 0 && (
                <p className="text-xs text-muted-foreground">
                  No codebases registered yet.{" "}
                  <Link to="/repos" className="underline underline-offset-2">
                    Add one
                  </Link>
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label>Model override (optional)</Label>
              <ModelSelect
                catalog={catalog}
                value={model}
                onChange={setModel}
                emptyLabel="Default from agent config"
              />
            </div>
          </div>
          <div className="max-w-48 space-y-2">
            <Label htmlFor="t-budget">Budget in USD (optional)</Label>
            <Input
              id="t-budget"
              type="number"
              min="0"
              step="0.5"
              placeholder="No cap"
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              The run aborts if LLM spend exceeds this.
            </p>
          </div>
          <Button onClick={submit} disabled={busy}>
            Queue task
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}
