import logging
import os

from importlib.metadata import version as _pkg_version
from mcp.server.fastmcp import FastMCP

from mcp_server.tools import docs
from mcp_server.resources import prompts

LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))

mcp = FastMCP("grandMA3 MCP Server", host="0.0.0.0", port=8000)


# --- Documentation tools (read) ---

@mcp.tool()
def list_topics() -> list[dict]:
    """List all indexed documentation topics."""
    return docs.list_topics()


@mcp.tool()
def get_doc(topic: str) -> str:
    """Return full markdown content for a documentation topic."""
    return docs.get_doc(topic)


@mcp.tool()
def search_docs(query: str) -> list[dict]:
    """Full-text search across all documentation files."""
    return docs.search_docs(query)


@mcp.tool()
def list_files() -> list[str]:
    """List all files in the docs directory."""
    return docs.list_files()


@mcp.tool()
def list_archives() -> list[str]:
    """List all archived document versions."""
    return docs.list_archives()


# --- Documentation tools (write with approval) ---

@mcp.tool()
def propose_doc_update(topic: str, new_content: str, description: str = "") -> dict:
    """Propose a doc update. Shows diff for user review before applying. User must confirm apply."""
    return docs.propose_doc_update(topic, new_content, description)


@mcp.tool()
def apply_doc_update(topic: str, description: str = "") -> dict:
    """Apply a proposed doc update: archives old version, writes new, updates index.json."""
    return docs.apply_doc_update(topic, description)


@mcp.tool()
def reject_proposal(topic: str) -> dict:
    """Reject a proposed doc update."""
    return docs.reject_proposal(topic)


# --- Prompt resources ---

@mcp.prompt()
def grandma3_expert() -> str:
    """Expert grandMA3 operator and Lua developer persona with show architecture context."""
    return prompts.grandma3_expert()



if __name__ == "__main__":
    logging.info("mcp library version: %s", _pkg_version("mcp"))
    mcp.run(transport="streamable-http")

