import logging
import os

from mcp.server.fastmcp import FastMCP

from mcp_server.tools import docs
from mcp_server.resources import prompts

LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))

# stateless_http: avoids server-side session tracking entirely, so an addon
# restart can never leave a client stuck retrying a now-unknown session ID.
mcp = FastMCP("grandMA3 MCP Server", host="0.0.0.0", port=8000, stateless_http=True)


# --- Documentation tools (read) ---

@mcp.tool()
def list_topics() -> list[dict]:
    """List all indexed documentation topics."""
    return docs.list_topics()


@mcp.tool()
def get_doc(topic: str, full: bool = False) -> dict:
    """Return a documentation topic's outline and summary by default; pass full=True for the complete markdown content."""
    return docs.get_doc(topic, full)


@mcp.tool()
def search_docs(query: str) -> dict:
    """Full-text search across all documentation files. Returns the top 5 matches with truncated excerpts and a total_matches count."""
    return docs.search_docs(query)


@mcp.tool()
def list_files() -> dict:
    """List all files in the docs directory, capped to the first 50 entries with a total count."""
    return docs.list_files()


@mcp.tool()
def list_archives() -> dict:
    """List all archived document versions, capped to the first 50 entries with a total count."""
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
    mcp.run(transport="streamable-http")

