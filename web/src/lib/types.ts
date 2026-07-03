export type TaskStatus =
  | "queued"
  | "running"
  | "awaiting_approval"
  | "completed"
  | "failed"
  | "cancelled"

export interface Task {
  id: string
  title: string
  description: string
  repo: string | null
  source: string
  source_ref: Record<string, unknown>
  status: TaskStatus
  branch: string | null
  pr_url: string | null
  error: string | null
  model: string | null
  parent_id: string | null
  budget_usd: number | null
  input_tokens: number | null
  output_tokens: number | null
  cost_usd: number | null
  created_at: number
  started_at: number | null
  finished_at: number | null
}

export interface TaskEvent {
  id: number
  task_id: string
  ts: number
  agent: string
  kind: string
  content: Record<string, any>
}

export interface Todo {
  content?: string
  title?: string
  status?: "pending" | "in_progress" | "completed"
}

export interface Stats {
  queued: number
  running: number
  completed: number
  failed: number
  cancelled: number
  awaiting_approval: number
  total: number
  spend_today_usd: number
  daily_budget_usd: number
  budget_paused: boolean
}

export interface StatusComponent {
  key: string
  label: string
  state: "ok" | "warn" | "down" | "off" | "unconfigured"
  detail: string
}

export interface StatusResponse {
  version: string
  components: StatusComponent[]
  config: {
    providers: Record<string, boolean>
    github: boolean
    memory: boolean
    firecrawl: boolean
    require_approval: boolean
    auth: boolean
    sandbox_default: boolean
    review_polling: boolean
    intakes: string[]
    running: number
  }
}

export interface RepoConfig {
  name: string
  url: string
  default_branch: string
  description?: string
  setup?: string | null
  test?: string | null
  context?: string[]
  secrets?: Record<string, string>
  sandbox?: boolean | null
  sandbox_network?: string | null
  approval?: string | null
}

/** provider -> { configured, models: ["provider:model", ...] } */
export type ModelCatalog = Record<string, { configured: boolean; models: string[] }>

export interface AgentSpec {
  model?: string | null
  effort?: string | null
  system_prompt?: string | null
  description?: string
}

export interface AgentsConfig {
  orchestrator?: AgentSpec
  subagents?: Record<string, AgentSpec>
  mcp_servers?: Record<string, McpServerSpec>
  pricing?: Record<string, unknown>
}

export interface McpServerSpec {
  transport?: string
  command?: string
  args?: string[]
  env?: Record<string, string>
  url?: string
  headers?: Record<string, string>
}

export interface Resource {
  id: string
  kind: string
  title?: string
  summary?: string
  status?: string
  scope: string
  url?: string
}

export interface ChatReply {
  session_id: string
  reply: string
  cost_usd?: number
}
