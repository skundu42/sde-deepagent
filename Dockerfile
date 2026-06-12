FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ripgrep curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY ui ./ui
COPY LICENSE README.md ./
RUN uv sync --frozen --no-dev

# config/, context/ and data/ are expected as volumes (see docker-compose.yml)
EXPOSE 8321
ENV HOST=0.0.0.0 PORT=8321

CMD ["uv", "run", "--no-sync", "sde-deepagent"]
