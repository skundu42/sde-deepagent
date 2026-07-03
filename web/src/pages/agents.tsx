import { useEffect, useState } from "react"
import { Bot, Plug, RotateCcw, Trash2 } from "lucide-react"
import { toast } from "sonner"

import { ModelSelect } from "@/components/model-select"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Input } from "@/components/ui/input"
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
import { parseKvLines } from "@/lib/format"
import type { AgentsConfig, McpServerSpec, ModelCatalog } from "@/lib/types"

// Mirrors agent_factory.ORCHESTRATOR_FORMAT_KEYS; shown as an editing hint.
const ORCH_PLACEHOLDERS = [
  "repo_name", "repo_description", "branch", "default_branch", "setup_cmd",
  "test_cmd", "task_description", "exec_environment", "ship_instructions",
  "repo_map", "context_block",
]
const EFFORTS = ["", "low", "medium", "high"]
const PROVIDER_DEFAULT = "__provider_default__"

function mcpSummary(spec: McpServerSpec): string {
  const t = spec.transport ?? (spec.command ? "stdio" : spec.url ? "streamable_http" : "?")
  if (spec.command) return `${t} · ${spec.command} ${(spec.args ?? []).join(" ")}`.trim()
  if (spec.url) return `${t} · ${spec.url}`
  return t
}

function PromptEditor({
  role,
  value,
  defaultValue,
  isOrchestrator,
  onChange,
}: {
  role: string
  value: string
  defaultValue: string
  isOrchestrator: boolean
  onChange: (v: string) => void
}) {
  const overridden = value !== defaultValue
  return (
    <Collapsible defaultOpen={overridden}>
      <div className="flex items-center gap-2">
        <CollapsibleTrigger className="text-xs font-medium text-muted-foreground hover:text-foreground">
          System prompt
        </CollapsibleTrigger>
        {overridden ? (
          <Badge variant="secondary" className="text-[10px]">overridden</Badge>
        ) : (
          <span className="text-[10px] text-muted-foreground">using built-in default</span>
        )}
      </div>
      <CollapsibleContent className="space-y-2 pt-2">
        {isOrchestrator && (
          <p className="text-xs text-muted-foreground">
            Placeholders (keep intact):{" "}
            {ORCH_PLACEHOLDERS.map((k) => (
              <code key={k} className="mr-1 rounded bg-muted px-1 font-mono text-[10px]">
                {"{" + k + "}"}
              </code>
            ))}
          </p>
        )}
        <Textarea
          rows={9}
          spellCheck={false}
          className="font-mono text-xs"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          aria-label={`${role} system prompt`}
        />
        <Button variant="outline" size="sm" onClick={() => onChange(defaultValue)}>
          <RotateCcw className="size-3.5" /> Reset to default
        </Button>
      </CollapsibleContent>
    </Collapsible>
  )
}

export default function AgentsPage() {
  const [cfg, setCfg] = useState<AgentsConfig | null>(null)
  const [catalog, setCatalog] = useState<ModelCatalog>({})
  const [defaults, setDefaults] = useState<Record<string, string>>({})
  // local editable prompt text per role (role -> text)
  const [prompts, setPrompts] = useState<Record<string, string>>({})
  const [mcpForm, setMcpForm] = useState({
    name: "", transport: "stdio", command: "", args: "", env: "", url: "", headers: "",
  })

  useEffect(() => {
    Promise.all([
      api<AgentsConfig>("/api/config/agents"),
      api<ModelCatalog>("/api/models"),
      api<Record<string, string>>("/api/config/prompt-defaults"),
    ])
      .then(([c, cat, defs]) => {
        c.mcp_servers ??= {}
        setCfg(c)
        setCatalog(cat)
        setDefaults(defs)
        const p: Record<string, string> = {
          orchestrator: c.orchestrator?.system_prompt ?? defs.orchestrator ?? "",
        }
        for (const [name, spec] of Object.entries(c.subagents ?? {}))
          p[name] = spec?.system_prompt ?? defs[name] ?? ""
        setPrompts(p)
      })
      .catch((e) => toast.error(e.message))
  }, [])

  if (!cfg) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-8 w-40" />
        {[...Array(3)].map((_, i) => (
          <Skeleton key={i} className="h-40 w-full" />
        ))}
      </div>
    )
  }

  const roles: Array<[string, boolean]> = [
    ["orchestrator", true],
    ...Object.keys(cfg.subagents ?? {}).map((n) => [n, false] as [string, boolean]),
  ]
  const specOf = (role: string) =>
    role === "orchestrator" ? (cfg.orchestrator ?? {}) : (cfg.subagents?.[role] ?? {})
  const patchSpec = (role: string, patch: Record<string, unknown>) =>
    setCfg((prev) => {
      if (!prev) return prev
      const next = { ...prev }
      if (role === "orchestrator") next.orchestrator = { ...next.orchestrator, ...patch }
      else next.subagents = {
        ...next.subagents,
        [role]: { ...next.subagents?.[role], ...patch },
      }
      return next
    })

  const addMcp = () => {
    const name = mcpForm.name.trim()
    if (!name) return toast.error("A server name is required")
    if (cfg.mcp_servers?.[name]) return toast.error("A server with that name already exists")
    let spec: McpServerSpec
    if (mcpForm.transport === "stdio") {
      const command = mcpForm.command.trim()
      if (!command) return toast.error("A command is required for stdio")
      spec = { command, transport: "stdio" }
      const args = mcpForm.args.split("\n").map((s) => s.trim()).filter(Boolean)
      const env = parseKvLines(mcpForm.env)
      if (args.length) spec.args = args
      if (Object.keys(env).length) spec.env = env
    } else {
      const url = mcpForm.url.trim()
      if (!url) return toast.error("A URL is required")
      spec = { url, transport: mcpForm.transport }
      const headers = parseKvLines(mcpForm.headers)
      if (Object.keys(headers).length) spec.headers = headers
    }
    setCfg((prev) => prev && { ...prev, mcp_servers: { ...prev.mcp_servers, [name]: spec } })
    setMcpForm({ name: "", transport: mcpForm.transport, command: "", args: "", env: "", url: "", headers: "" })
    toast.success("Server added. Save to persist.")
  }

  const removeMcp = (name: string) =>
    setCfg((prev) => {
      if (!prev) return prev
      const servers = { ...prev.mcp_servers }
      delete servers[name]
      return { ...prev, mcp_servers: servers }
    })

  const save = async () => {
    const payload: AgentsConfig = JSON.parse(JSON.stringify(cfg))
    for (const [role] of roles) {
      const text = prompts[role] ?? ""
      // empty or unchanged from the default means "no override"
      const override = text.trim() === "" || text === (defaults[role] ?? "") ? null : text
      if (role === "orchestrator")
        payload.orchestrator = { ...payload.orchestrator, system_prompt: override }
      else if (payload.subagents?.[role])
        payload.subagents[role] = { ...payload.subagents[role], system_prompt: override }
    }
    try {
      await api("/api/config/agents", { method: "PUT", body: JSON.stringify(payload) })
      toast.success("Agent config saved")
    } catch (e: any) {
      toast.error(e.message)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">Agents</h1>
        <span className="text-sm text-muted-foreground">
          Models, prompts and MCP servers. Applies to the next task.
        </span>
        <Button className="ml-auto" onClick={save}>Save</Button>
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        {roles.map(([role, isOrch]) => {
          const spec = specOf(role)
          return (
            <Card key={role} className={isOrch ? "lg:col-span-2" : undefined}>
              <CardHeader className="pb-2">
                <CardTitle className="flex items-center gap-2 text-base">
                  <Bot className="size-4" />
                  {role}
                  <span className="text-xs font-normal text-muted-foreground">
                    {isOrch ? "coordinates everything" : "subagent"}
                  </span>
                </CardTitle>
                {(spec.description || isOrch) && (
                  <CardDescription>
                    {spec.description ||
                      "Plans the task, delegates to subagents, ships the pull request."}
                  </CardDescription>
                )}
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-2">
                    <Label>Model</Label>
                    <ModelSelect
                      catalog={catalog}
                      value={spec.model ?? null}
                      onChange={(v) => patchSpec(role, { model: v })}
                      emptyLabel={isOrch ? undefined : "Inherit from orchestrator"}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label>Reasoning effort</Label>
                    <Select
                      value={spec.effort || PROVIDER_DEFAULT}
                      onValueChange={(v) =>
                        patchSpec(role, { effort: v === PROVIDER_DEFAULT ? null : v })
                      }
                    >
                      <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        {EFFORTS.map((e) => (
                          <SelectItem key={e || PROVIDER_DEFAULT} value={e || PROVIDER_DEFAULT}>
                            {e || "Provider default"}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                </div>
                <PromptEditor
                  role={role}
                  value={prompts[role] ?? ""}
                  defaultValue={defaults[role] ?? ""}
                  isOrchestrator={isOrch}
                  onChange={(v) => setPrompts((p) => ({ ...p, [role]: v }))}
                />
              </CardContent>
            </Card>
          )
        })}
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-base">
            <Plug className="size-4" /> MCP servers
            <span className="text-xs font-normal text-muted-foreground">
              extra tools for the orchestrator
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {Object.keys(cfg.mcp_servers ?? {}).length === 0 ? (
            <p className="text-sm text-muted-foreground">No MCP servers configured.</p>
          ) : (
            <div className="space-y-2">
              {Object.entries(cfg.mcp_servers ?? {}).map(([name, spec]) => (
                <div key={name} className="flex items-center gap-3 rounded-md border px-3 py-2">
                  <span className="text-sm font-medium">{name}</span>
                  <span className="truncate font-mono text-xs text-muted-foreground">
                    {mcpSummary(spec)}
                  </span>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="ml-auto"
                    aria-label={`Remove ${name}`}
                    onClick={() => removeMcp(name)}
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </div>
              ))}
            </div>
          )}

          <div className="grid max-w-xl gap-3">
            <div className="grid gap-3 sm:grid-cols-[1fr_11rem]">
              <Input
                placeholder="Name (e.g. github)"
                value={mcpForm.name}
                onChange={(e) => setMcpForm((f) => ({ ...f, name: e.target.value }))}
              />
              <Select
                value={mcpForm.transport}
                onValueChange={(v) => setMcpForm((f) => ({ ...f, transport: v }))}
              >
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {["stdio", "streamable_http", "sse"].map((t) => (
                    <SelectItem key={t} value={t}>{t}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {mcpForm.transport === "stdio" ? (
              <>
                <Input
                  placeholder="Command (e.g. npx)"
                  value={mcpForm.command}
                  onChange={(e) => setMcpForm((f) => ({ ...f, command: e.target.value }))}
                />
                <Textarea
                  rows={2}
                  placeholder="Args, one per line"
                  value={mcpForm.args}
                  onChange={(e) => setMcpForm((f) => ({ ...f, args: e.target.value }))}
                />
                <Textarea
                  rows={2}
                  placeholder="Env, KEY=value per line"
                  value={mcpForm.env}
                  onChange={(e) => setMcpForm((f) => ({ ...f, env: e.target.value }))}
                />
              </>
            ) : (
              <>
                <Input
                  placeholder="URL (https://.../mcp)"
                  value={mcpForm.url}
                  onChange={(e) => setMcpForm((f) => ({ ...f, url: e.target.value }))}
                />
                <Textarea
                  rows={2}
                  placeholder="Headers, KEY=value per line"
                  value={mcpForm.headers}
                  onChange={(e) => setMcpForm((f) => ({ ...f, headers: e.target.value }))}
                />
              </>
            )}
            <div>
              <Button variant="outline" size="sm" onClick={addMcp}>Add server</Button>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
