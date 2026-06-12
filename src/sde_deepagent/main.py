"""Entrypoint: `sde-deepagent` (or `uv run sde-deepagent`) starts the server, the worker
pool, and every configured intake channel in one process."""

from __future__ import annotations

import logging

import uvicorn

from .server import create_app
from .settings import get_settings

app = create_app()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = get_settings()
    if not any([settings.anthropic_api_key, settings.google_api_key,
                settings.openai_api_key]):
        logging.warning(
            "No model API key configured! Set ANTHROPIC_API_KEY, GOOGLE_API_KEY "
            "and/or OPENAI_API_KEY in .env — tasks will fail until you do."
        )
    uvicorn.run("sde_deepagent.main:app", host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
