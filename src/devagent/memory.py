"""Long-term memory backed by a self-hosted Supermemory instance
(https://supermemory.ai/docs/self-hosting/overview — `npx supermemory local`).

Memories are partitioned by container tag:
  devagent_global          org-wide learnings, shared across all codebases
  devagent_repo_<name>     learnings about one codebase

Failures are never fatal: a down memory server degrades to "no memory",
logged once per operation."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .settings import Settings

logger = logging.getLogger(__name__)

GLOBAL_TAG = "devagent_global"


def repo_tag(repo_name: str) -> str:
    return "devagent_repo_" + re.sub(r"[^a-zA-Z0-9_-]", "-", repo_name)


class Memory:
    def __init__(self, base_url: str, api_key: str, timeout: float = 20.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._transport = transport  # injectable for tests

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout or self.timeout,
                                 transport=self._transport)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    async def add(self, content: str, container_tag: str,
                  metadata: dict[str, Any] | None = None) -> str | None:
        """Store one memory. Returns the memory id, or None on failure."""
        payload: dict[str, Any] = {"content": content, "containerTag": container_tag}
        if metadata:
            payload["metadata"] = metadata
        try:
            async with self._client() as client:
                resp = await client.post(f"{self.base_url}/v3/documents",
                                         headers=self._headers(), json=payload)
                resp.raise_for_status()
                return resp.json().get("id")
        except Exception as e:  # noqa: BLE001 — memory must never break a task
            logger.warning("supermemory add failed: %s", e)
            return None

    async def search(self, query: str, container_tags: list[str],
                     limit: int = 5) -> list[dict[str, Any]]:
        """Search the given containers, merged and sorted by similarity."""
        results: list[dict[str, Any]] = []
        async with self._client() as client:
            for tag in container_tags:
                try:
                    # hybrid searches extracted memories AND raw chunks, so
                    # results come back even while LLM extraction is pending
                    # or unavailable (local-embedding-only setups)
                    resp = await client.post(
                        f"{self.base_url}/v4/search", headers=self._headers(),
                        json={"q": query, "containerTag": tag, "limit": limit,
                              "searchMode": "hybrid"},
                    )
                    resp.raise_for_status()
                    for r in resp.json().get("results", []):
                        text = r.get("memory") or r.get("chunk") or ""
                        if text:
                            results.append({
                                "memory": text,
                                "similarity": r.get("similarity"),
                                "metadata": r.get("metadata") or {},
                                "container": tag,
                            })
                except Exception as e:  # noqa: BLE001
                    logger.warning("supermemory search failed for %s: %s", tag, e)
        results.sort(key=lambda r: r.get("similarity") or 0, reverse=True)
        return results[:limit]

    async def list_documents(self, container_tags: list[str],
                             limit: int = 100) -> list[dict[str, Any]]:
        """List stored documents (newest first) across the given containers."""
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"{self.base_url}/v3/documents/list", headers=self._headers(),
                    json={"containerTags": container_tags, "limit": limit,
                          "sort": "createdAt", "order": "desc"},
                )
                resp.raise_for_status()
                return resp.json().get("memories") or []
        except Exception as e:  # noqa: BLE001
            logger.warning("supermemory list failed: %s", e)
            return []

    async def delete_document(self, doc_id: str) -> bool:
        try:
            async with self._client() as client:
                resp = await client.delete(
                    f"{self.base_url}/v3/documents/{doc_id}", headers=self._headers())
                return resp.status_code < 300
        except Exception as e:  # noqa: BLE001
            logger.warning("supermemory delete failed: %s", e)
            return False

    async def ping(self) -> bool:
        try:
            async with self._client(timeout=5) as client:
                resp = await client.post(
                    f"{self.base_url}/v4/search", headers=self._headers(),
                    json={"q": "ping", "containerTag": GLOBAL_TAG, "limit": 1},
                )
                return resp.status_code < 500
        except Exception:  # noqa: BLE001
            return False


def memory_from_settings(settings: Settings) -> Memory | None:
    if settings.supermemory_base_url and settings.supermemory_api_key:
        return Memory(settings.supermemory_base_url, settings.supermemory_api_key)
    return None
