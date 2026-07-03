import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    // UI dev against a locally running sde-deepagent server
    proxy: {
      "/api": "http://127.0.0.1:8321",
      "/webhooks": "http://127.0.0.1:8321",
    },
  },
})
