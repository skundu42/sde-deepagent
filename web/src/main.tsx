import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { createHashRouter, RouterProvider } from "react-router-dom"

import Layout from "@/components/layout"
import { ThemeProvider } from "@/components/theme"
import { Toaster } from "@/components/ui/sonner"
import AgentsPage from "@/pages/agents"
import ChatPage from "@/pages/chat"
import NewTaskPage from "@/pages/new-task"
import ReposPage from "@/pages/repos"
import ResourcesPage from "@/pages/resources"
import StatusPage from "@/pages/status"
import TaskDetailPage from "@/pages/task-detail"
import TasksPage from "@/pages/tasks"

import "./index.css"

// Hash routing keeps deep links working from a plain static file server
// (FastAPI's StaticFiles has no history-API fallback) and matches the old
// UI's #/task/... link shape.
const router = createHashRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <TasksPage /> },
      { path: "new", element: <NewTaskPage /> },
      { path: "tasks/:id", element: <TaskDetailPage /> },
      { path: "task/:id", element: <TaskDetailPage /> }, // old bookmark shape
      { path: "repos", element: <ReposPage /> },
      { path: "agents", element: <AgentsPage /> },
      { path: "chat", element: <ChatPage /> },
      { path: "resources", element: <ResourcesPage /> },
      { path: "status", element: <StatusPage /> },
    ],
  },
])

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <RouterProvider router={router} />
      <Toaster richColors position="bottom-right" />
    </ThemeProvider>
  </StrictMode>,
)
