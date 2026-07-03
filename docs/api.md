# HTTP API

The web UI is a thin client over a plain REST API. With `AUTH_TOKEN` set,
every request, including the SSE streams, needs
`Authorization: Bearer <token>`. Query-string tokens are not accepted: they
would leak into server and proxy access logs. Consume the SSE endpoints with
a streaming `fetch()` (as the web UI does) rather than `EventSource`, which
cannot send headers. `/api/health` and `/webhooks/*` are public.

```
GET  /api/health             GET  /api/stats              GET  /api/models
GET  /api/status             GET  /api/tasks              POST /api/tasks
GET  /api/tasks/{id}         POST /api/tasks/{id}/cancel  POST /api/tasks/{id}/steer
POST /api/tasks/{id}/approve POST /api/tasks/{id}/reject
GET  /api/tasks/{id}/events  GET  /api/tasks/{id}/stream  (SSE)
GET  /api/stream (SSE)       GET/POST/DELETE /api/repos
GET/PUT /api/config/agents   GET  /api/config/prompt-defaults
POST /api/chat               DELETE /api/chat/{id}
GET/POST/DELETE /api/resources                            POST /webhooks/linear
```

Notes:

- `POST /api/tasks` takes
  `{title, description, repo?, model?, budget_usd?, parent_id?}`;
  `parent_id` queues a revision that continues the parent's branch and PR.
- `GET /api/tasks/{id}/stream?after=<event_id>` resumes the live stream after
  the last replayed event.
- `GET /api/status` returns per-component health plus a `config` block (the
  deployment fingerprint; this is deliberately not on the public
  `/api/health`).
- Repo secrets are write-only: `PUT /api/repos/{name}/secrets` stores values
  encrypted; nothing ever returns them.
