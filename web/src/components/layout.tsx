import { useCallback, useEffect, useRef, useState } from "react"
import { NavLink, Outlet, useLocation } from "react-router-dom"
import {
  Activity,
  BookOpen,
  Bot,
  FolderGit2,
  KeyRound,
  ListTodo,
  Menu,
  MessageSquare,
  Moon,
  Plus,
  Sun,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet"
import { useTheme } from "@/components/theme"
import { api, auth, sseUrl } from "@/lib/api"
import { cn } from "@/lib/utils"
import type { Stats } from "@/lib/types"

const NAV = [
  { to: "/", label: "Tasks", icon: ListTodo, end: true },
  { to: "/new", label: "New task", icon: Plus },
  { to: "/repos", label: "Codebases", icon: FolderGit2 },
  { to: "/agents", label: "Agents", icon: Bot },
  { to: "/chat", label: "Chat", icon: MessageSquare },
  { to: "/resources", label: "Resources", icon: BookOpen },
  { to: "/status", label: "Status", icon: Activity },
]

/** Fires whenever a status event arrives on the global stream; the task list
 * subscribes to stay live without owning the connection. */
export const taskEvents = new EventTarget()

function Nav({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav className="grid gap-1 px-2">
      {NAV.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground",
              isActive && "bg-accent text-accent-foreground",
            )
          }
        >
          <Icon className="size-4" />
          {label}
        </NavLink>
      ))}
    </nav>
  )
}

function SpendLine({ stats }: { stats: Stats | null }) {
  if (!stats) return null
  const over80 =
    stats.daily_budget_usd > 0 && stats.spend_today_usd >= 0.8 * stats.daily_budget_usd
  return (
    <div className="space-y-1.5 border-t px-4 py-3 text-xs text-muted-foreground">
      <div className="flex justify-between">
        <span>Running</span>
        <span className="font-mono">{stats.running}</span>
      </div>
      <div className="flex justify-between">
        <span>Queued</span>
        <span className="font-mono">{stats.queued}</span>
      </div>
      <div className="flex justify-between">
        <span>Spend today</span>
        <span
          className={cn(
            "font-mono",
            stats.budget_paused ? "text-red-500" : over80 ? "text-amber-500" : "",
          )}
        >
          ${stats.spend_today_usd.toFixed(2)}
          {stats.daily_budget_usd > 0 ? ` / $${stats.daily_budget_usd.toFixed(0)}` : ""}
        </span>
      </div>
      {stats.budget_paused && (
        <div className="text-red-500">Queue paused: daily budget reached</div>
      )}
    </div>
  )
}

function ThemeToggle() {
  const { theme, setTheme } = useTheme()
  const dark =
    theme === "dark" ||
    (theme === "system" &&
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches)
  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label="Toggle theme"
      onClick={() => setTheme(dark ? "light" : "dark")}
    >
      {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
    </Button>
  )
}

function TokenDialog({
  open,
  onSave,
  onClose,
}: {
  open: boolean
  onSave: (token: string) => void
  onClose: () => void
}) {
  const [value, setValue] = useState("")
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <KeyRound className="size-4" /> API token required
          </DialogTitle>
          <DialogDescription>
            This server has authentication enabled. Paste the AUTH_TOKEN it was
            started with; it is kept for this browser session only.
          </DialogDescription>
        </DialogHeader>
        <form
          onSubmit={(e) => {
            e.preventDefault()
            if (value.trim()) onSave(value.trim())
          }}
        >
          <Input
            autoFocus
            type="password"
            placeholder="token"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          <DialogFooter className="mt-4">
            <Button type="submit" disabled={!value.trim()}>
              Save token
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export default function Layout() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [connected, setConnected] = useState(false)
  const [needToken, setNeedToken] = useState(false)
  const [authVersion, setAuthVersion] = useState(0)
  const [mobileOpen, setMobileOpen] = useState(false)
  const esRef = useRef<EventSource | null>(null)
  const location = useLocation()

  const refreshStats = useCallback(() => {
    api<Stats>("/api/stats").then(setStats).catch(() => {})
  }, [])

  // global SSE: connection dot, live stats, task-list refresh signal
  useEffect(() => {
    const es = new EventSource(sseUrl("/api/stream"))
    esRef.current = es
    es.onopen = () => setConnected(true)
    es.onerror = () => setConnected(false)
    es.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data)
        if (ev.kind === "status") {
          refreshStats()
          taskEvents.dispatchEvent(new CustomEvent("task-status"))
        }
      } catch {
        /* ignore malformed frames */
      }
    }
    refreshStats()
    const timer = setInterval(refreshStats, 30_000)
    return () => {
      es.close()
      clearInterval(timer)
    }
  }, [refreshStats, authVersion])

  // any 401 anywhere opens the token dialog; saving re-keys the whole app so
  // views re-fetch and SSE streams reconnect with the new token
  useEffect(() => auth.on("unauthorized", () => setNeedToken(true)), [])

  return (
    <div className="flex min-h-svh">
      <aside className="sticky top-0 hidden h-svh w-56 shrink-0 flex-col border-r bg-sidebar md:flex">
        <div className="flex items-center gap-2 px-4 py-4">
          <Bot className="size-5" />
          <div className="leading-tight">
            <div className="text-sm font-semibold">sde-deepagent</div>
            <div className="text-xs text-muted-foreground">mission control</div>
          </div>
        </div>
        <Nav />
        <div className="mt-auto">
          <SpendLine stats={stats} />
          <div className="flex items-center justify-between border-t px-4 py-2">
            <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <span
                className={cn(
                  "size-1.5 rounded-full",
                  connected ? "bg-emerald-500" : "bg-red-500",
                )}
              />
              {connected ? "live" : "offline"}
            </span>
            <ThemeToggle />
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-2 border-b px-4 py-2 md:hidden">
          <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
            <SheetTrigger asChild>
              <Button variant="ghost" size="icon" aria-label="Open navigation">
                <Menu className="size-4" />
              </Button>
            </SheetTrigger>
            <SheetContent side="left" className="w-64 p-0 pt-8">
              <Nav onNavigate={() => setMobileOpen(false)} />
              <div className="mt-4">
                <SpendLine stats={stats} />
              </div>
            </SheetContent>
          </Sheet>
          <span className="text-sm font-semibold">sde-deepagent</span>
          <span className="ml-auto">
            <ThemeToggle />
          </span>
        </header>
        <main
          key={`${authVersion}:${location.key}`}
          className="mx-auto w-full max-w-5xl flex-1 px-4 py-6 md:px-8"
        >
          <Outlet />
        </main>
      </div>

      <TokenDialog
        open={needToken}
        onClose={() => setNeedToken(false)}
        onSave={(t) => {
          auth.set(t)
          setNeedToken(false)
          setAuthVersion((v) => v + 1)
        }}
      />
    </div>
  )
}
