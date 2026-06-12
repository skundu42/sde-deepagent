"""Loads extra tools from MCP servers declared in config/agents.yaml."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def load_mcp_tools(servers: dict[str, dict[str, Any]]) -> list:
    """Return LangChain tools for every configured MCP server.
    A misconfigured/unreachable server is logged and skipped, never fatal."""
    if not servers:
        return []
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logger.warning("langchain-mcp-adapters not installed; skipping MCP servers")
        return []
    try:
        client = MultiServerMCPClient(dict(servers))
        tools = await client.get_tools()
        logger.info("loaded %d MCP tools from %d servers", len(tools), len(servers))
        return tools
    except Exception:
        logger.exception("failed to load MCP tools; continuing without them")
        return []
