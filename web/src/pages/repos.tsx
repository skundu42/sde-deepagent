import { useEffect, useState } from "react"
import { FolderGit2, Lock, ShieldCheck, Trash2, X } from "lucide-react"
import { toast } from "sonner"

import { Empty } from "@/components/empty"
import { Badge } from "@/components/ui/badge"
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
import { parseSecretLines } from "@/lib/format"
import type { RepoConfig } from "@/lib/types"

const DEFAULT = "__default__"

const emptyForm = {
  name: "",
  url: "",
  branch: "main",
  description: "",
  setup: "",
  test: "",
  context: "",
  secrets: "",
  sandbox: DEFAULT,
  network: DEFAULT,
  approval: DEFAULT,
}

export default function ReposPage() {
  const [repos, setRepos] = useState<Record<string, RepoConfig> | null>(null)
  const [form, setForm] = useState(emptyForm)
  const set = (k: keyof typeof emptyForm) => (v: string) =>
    setForm((f) => ({ ...f, [k]: v }))

  const load = () =>
    api<Record<string, RepoConfig>>("/api/repos")
      .then(setRepos)
      .catch((e) => toast.error(e.message))
  useEffect(() => {
    load()
  }, [])

  const removeRepo = async (name: string) => {
    try {
      await api(`/api/repos/${encodeURIComponent(name)}`, { method: "DELETE" })
      load()
    } catch (e: any) {
      toast.error(e.message)
    }
  }

  const removeSecret = async (repo: string, name: string) => {
    try {
      await api(
        `/api/repos/${encodeURIComponent(repo)}/secrets/${encodeURIComponent(name)}`,
        { method: "DELETE" },
      )
      load()
    } catch (e: any) {
      toast.error(e.message)
    }
  }

  const save = async () => {
    const name = form.name.trim()
    const { refs, values } = parseSecretLines(form.secrets)
    try {
      await api("/api/repos", {
        method: "POST",
        body: JSON.stringify({
          name,
          url: form.url.trim(),
          default_branch: form.branch.trim() || "main",
          description: form.description.trim(),
          setup: form.setup.trim() || null,
          test: form.test.trim() || null,
          context: form.context.split(",").map((s) => s.trim()).filter(Boolean),
          sandbox: { true: true, false: false }[form.sandbox] ?? null,
          sandbox_network: form.network === DEFAULT ? null : form.network,
          approval: form.approval === DEFAULT ? null : form.approval,
          secrets: refs,
        }),
      })
      if (Object.keys(values).length) {
        await api(`/api/repos/${encodeURIComponent(name)}/secrets`, {
          method: "PUT",
          body: JSON.stringify({ values }),
        })
      }
      toast.success("Codebase saved")
      setForm(emptyForm)
      load()
    } catch (e: any) {
      toast.error(e.message)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">Codebases</h1>
        <span className="text-sm text-muted-foreground">
          Repositories the agent can work on
        </span>
      </div>

      {repos && Object.keys(repos).length === 0 && (
        <Empty icon={FolderGit2} title="No codebases yet">
          Register the repositories your agents should work on.
        </Empty>
      )}

      {repos && Object.keys(repos).length > 0 && (
        <div className="grid gap-3 lg:grid-cols-2">
          {Object.entries(repos).map(([name, r]) => (
            <Card key={name}>
              <CardHeader className="pb-2">
                <CardTitle className="flex items-center justify-between text-base">
                  {name}
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`Delete ${name}`}
                    onClick={() => removeRepo(name)}
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </CardTitle>
                <CardDescription className="truncate font-mono text-xs">{r.url}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                {r.description && <p className="text-muted-foreground">{r.description}</p>}
                <div className="grid gap-1 text-xs">
                  <div>
                    <span className="text-muted-foreground">branch</span>{" "}
                    <span className="font-mono">{r.default_branch}</span>
                  </div>
                  {r.setup && (
                    <div>
                      <span className="text-muted-foreground">setup</span>{" "}
                      <span className="font-mono">{r.setup}</span>
                    </div>
                  )}
                  {r.test && (
                    <div>
                      <span className="text-muted-foreground">test</span>{" "}
                      <span className="font-mono">{r.test}</span>
                    </div>
                  )}
                  {(r.context ?? []).length > 0 && (
                    <div>
                      <span className="text-muted-foreground">docs</span>{" "}
                      <span className="font-mono">{r.context!.join(", ")}</span>
                    </div>
                  )}
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(r.secrets ?? {}).map(([k, v]) => (
                    <Badge key={k} variant="outline" className="gap-1">
                      {v === "store" ? <Lock className="size-3" /> : null}
                      {k}
                      {v !== "store" ? `=${v}` : ""}
                      <button
                        aria-label={`Remove secret ${k}`}
                        onClick={() => removeSecret(name, k)}
                        className="opacity-60 hover:opacity-100"
                      >
                        <X className="size-3" />
                      </button>
                    </Badge>
                  ))}
                  {r.sandbox && (
                    <Badge variant="secondary" className="gap-1">
                      <ShieldCheck className="size-3" />
                      sandboxed{r.sandbox_network ? `: ${r.sandbox_network}` : ""}
                    </Badge>
                  )}
                  {r.approval === "required" && <Badge variant="secondary">approval required</Badge>}
                  {r.approval === "auto" && <Badge variant="outline">auto-ship</Badge>}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <Card className="max-w-2xl">
        <CardHeader>
          <CardTitle>Register a codebase</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="r-name">Name</Label>
              <Input id="r-name" placeholder="backend" value={form.name}
                onChange={(e) => set("name")(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="r-branch">Default branch</Label>
              <Input id="r-branch" value={form.branch}
                onChange={(e) => set("branch")(e.target.value)} />
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="r-url">Git URL or local path</Label>
            <Input id="r-url" placeholder="git@github.com:acme/backend.git" value={form.url}
              onChange={(e) => set("url")(e.target.value)} />
            <p className="text-xs text-muted-foreground">
              https://, git@, or an absolute local path. Pull requests need a
              GitHub-style remote and GITHUB_TOKEN.
            </p>
          </div>
          <div className="space-y-2">
            <Label htmlFor="r-desc">Description</Label>
            <Input id="r-desc" placeholder="Python FastAPI monolith serving the public API"
              value={form.description} onChange={(e) => set("description")(e.target.value)} />
            <p className="text-xs text-muted-foreground">Helps route tasks to the right repo.</p>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="r-setup">Setup command</Label>
              <Input id="r-setup" placeholder="uv sync" value={form.setup}
                onChange={(e) => set("setup")(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="r-test">Test command</Label>
              <Input id="r-test" placeholder="uv run pytest -x -q" value={form.test}
                onChange={(e) => set("test")(e.target.value)} />
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="r-ctx">Context docs (comma-separated globs)</Label>
            <Input id="r-ctx" placeholder="docs/architecture.md, CONTRIBUTING.md"
              value={form.context} onChange={(e) => set("context")(e.target.value)} />
          </div>
          <div className="space-y-2">
            <Label htmlFor="r-secrets">Secrets (one per line)</Label>
            <Textarea
              id="r-secrets"
              rows={2}
              placeholder={"DATABASE_URL=postgres://user:pass@host/db\nREGISTRY_TOKEN=env:NPM_TOKEN"}
              value={form.secrets}
              onChange={(e) => set("secrets")(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              NAME=value is stored encrypted (needs SECRETS_KEY); NAME=env:HOST_VAR
              references the server environment. Injected only into setup and test
              commands, never into the agent.
            </p>
          </div>
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="space-y-2">
              <Label>Sandbox</Label>
              <Select value={form.sandbox} onValueChange={set("sandbox")}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value={DEFAULT}>Server default</SelectItem>
                  <SelectItem value="true">On: isolate shell and egress</SelectItem>
                  <SelectItem value="false">Off: run on host</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>Sandbox network</Label>
              <Select value={form.network} onValueChange={set("network")}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value={DEFAULT}>Server default (bridge)</SelectItem>
                  <SelectItem value="none">None: no egress</SelectItem>
                  <SelectItem value="bridge">Bridge: allow egress</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>Approval policy</Label>
              <Select value={form.approval} onValueChange={set("approval")}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value={DEFAULT}>Server default</SelectItem>
                  <SelectItem value="auto">Auto-ship (open PR directly)</SelectItem>
                  <SelectItem value="required">Require human approval</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <Button onClick={save}>Save codebase</Button>
        </CardContent>
      </Card>
    </div>
  )
}
