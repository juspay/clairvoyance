"""
MCP (Model Context Protocol) integration for Breeze Buddy.

This module provides the MCP client for template-based tool management.
When enabled in a template's configuration, MCP tools are automatically
fetched from the Storefront MCP and registered with the LLM.

Usage:
    from app.ai.voice.agents.breeze_buddy.mcp import BreezeBuddyMCPClient

    client = BreezeBuddyMCPClient()
    await client.connect(
        server_url="https://your-store.myshopify.com/api/mcp",
        timeout=30,  # seconds (optional)
    )
    tools = await client.register_tools(llm)
"""

from app.ai.voice.agents.breeze_buddy.mcp.client import BreezeBuddyMCPClient

__all__ = ["BreezeBuddyMCPClient"]
