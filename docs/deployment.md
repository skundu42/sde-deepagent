# Deployment

## Local (bare metal)

```bash
git clone https://github.com/skundu42/sde-deepagent.git && cd sde-deepagent
cp .env.example .env        # add at least one model key, plus GITHUB_TOKEN to open PRs
uv sync
uv run sde-deepagent        # then open http://localhost:8321
```

Docker must be running (the default sandboxed execution path) unless you set
`SANDBOX_DEFAULT=false`. No Node.js needed: the built UI ships in the repo.

## Docker Compose (production)

Uses the prebuilt multi-arch image from GitHub Container Registry
([`ghcr.io/skundu42/sde-deepagent`](https://github.com/skundu42/sde-deepagent/pkgs/container/sde-deepagent)),
so nothing compiles on your box:

```bash
git clone https://github.com/skundu42/sde-deepagent.git && cd sde-deepagent
cp .env.example .env        # set AUTH_TOKEN, SDE_DATA_DIR (absolute path), a model key, GITHUB_TOKEN
docker compose up -d        # pulls ghcr.io/skundu42/sde-deepagent:latest

# first boot only: copy supermemory's generated key into .env, then recreate
docker compose logs supermemory | grep "api key"
echo 'SUPERMEMORY_API_KEY=sm_...' >> .env && docker compose up -d
```

- **Pin a version** by setting `SDE_IMAGE_TAG=0.3.0` in `.env`. Available tags:
  `X.Y.Z` and `X.Y` per release, `latest` (newest release), `edge` (rolling
  build of `main`).
- **Build from source** instead of pulling: `docker compose up -d --build`.

### Security posture

- Compose publishes both ports on localhost only and refuses to start without
  `AUTH_TOKEN`; front it with a TLS reverse proxy for remote access.
- On a server, set `REQUIRE_APPROVAL=true` and `DAILY_BUDGET_USD`.
- The controller mounts the host Docker socket to launch sandbox containers.
  Access to the control plane is therefore host-root-equivalent: treat the
  `AUTH_TOKEN` accordingly.

### Sidecars

- **Supermemory** (long-term memory) runs as a compose service by default.
  Remove the service and the `SUPERMEMORY_*` variables if you do not want
  memory.
- **Firecrawl** (JS-rendering scraper for the Resources page) is optional and
  runs from its own repo's compose; point `FIRECRAWL_API_URL` at it. Without
  it, the built-in fetcher handles static pages.

## First steps after boot

In the UI: **Codebases** > register a repo (git URL or local path, test
command) > **New task** > watch the live trace; the PR link appears when it
ships.
