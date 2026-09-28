import hmac
import json
import logging
import os
from pathlib import Path

import uvicorn
from mcp.server.fastmcp import FastMCP

from mcp_server.tools import docs
from mcp_server.resources import prompts


def _options() -> dict:
    """Add-on options (HA writes them to /data/options.json); env vars override."""
    opts = {}
    try:
        opts = json.loads(Path("/data/options.json").read_text())
    except Exception:  # noqa: BLE001 -- running outside HA
        pass
    if os.environ.get("MCP_AUTH_TOKEN") is not None:
        opts["auth_token"] = os.environ["MCP_AUTH_TOKEN"]
    if os.environ.get("LOG_LEVEL"):
        opts["log_level"] = os.environ["LOG_LEVEL"]
    return opts


OPTIONS = _options()
LOG_LEVEL = str(OPTIONS.get("log_level") or "info").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("grandma3-mcp")

# stateless_http: avoids server-side session tracking entirely, so an addon
# restart can never leave a client stuck retrying a now-unknown session ID.
mcp = FastMCP("grandMA3 MCP Server", host="0.0.0.0", port=8000, stateless_http=True)


# --- Documentation tools (read) ---

@mcp.tool()
def list_topics() -> list[dict]:
    """List all documentation topics with a one-line summary of what each covers."""
    return docs.list_topics()


@mcp.tool()
def get_doc(topic: str, full: bool = False) -> dict:
    """Return a topic's frontmatter, outline and summary by default; pass full=True for the complete markdown content."""
    return docs.get_doc(topic, full)


@mcp.tool()
def search_docs(query: str) -> dict:
    """Full-text search across all documentation topics. Returns the top 5 matches with truncated excerpts and a total_matches count."""
    return docs.search_docs(query)


@mcp.tool()
def list_files(offset: int = 0) -> dict:
    """List files in the docs directory, 50 per page (use offset), with a total count."""
    return docs.list_files(offset)


@mcp.tool()
def list_archives(topic: str = "", offset: int = 0) -> dict:
    """List archived document versions, newest first, 50 per page; pass topic to see one topic's history."""
    return docs.list_archives(topic, offset)


@mcp.tool()
def get_archive(name: str) -> dict:
    """Return the content of one archived version, by the name list_archives returned."""
    return docs.get_archive(name)


@mcp.tool()
def list_proposals() -> dict:
    """List pending (unapplied) doc proposals; stale=true means the topic changed since the draft."""
    return docs.list_proposals()


# --- Documentation tools (write with approval) ---

@mcp.tool()
def propose_doc_update(topic: str, new_content: str, description: str = "", summary: str = "") -> dict:
    """Propose a doc update and return the diff for user review. description = what this edit changes; summary = optional one-line description of what the whole topic covers (kept in the index). Nothing changes until apply_doc_update."""
    return docs.propose_doc_update(topic, new_content, description, summary)


@mcp.tool()
def apply_doc_update(topic: str, description: str = "") -> dict:
    """Apply a proposed doc update the user approved: archives the old version, writes the new one, updates the index. Refused if the topic changed after the proposal was drafted."""
    return docs.apply_doc_update(topic, description)


@mcp.tool()
def reject_proposal(topic: str) -> dict:
    """Reject (discard) a proposed doc update."""
    return docs.reject_proposal(topic)


# --- Prompt resources ---

@mcp.prompt()
def grandma3_expert() -> str:
    """Expert grandMA3 operator and Lua developer persona with show architecture context."""
    return prompts.grandma3_expert()


class BearerAuth:
    """Pure-ASGI bearer-token check. Lifespan and other non-HTTP scopes pass
    straight through, so the streamable-HTTP session manager still starts."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"unauthorized\n"})
                return
        await self.app(scope, receive, send)


def main():
    docs.reconcile_index()
    app = mcp.streamable_http_app()
    token = str(OPTIONS.get("auth_token") or "")
    if token:
        app = BearerAuth(app, token)
        log.info("bearer-token auth ENABLED")
    else:
        log.warning("auth_token is empty: the server accepts unauthenticated requests (read AND write)")
    try:
        import importlib.metadata as md
        log.info("mcp package %s", md.version("mcp"))
    except Exception:  # noqa: BLE001
        pass
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "8000")), log_level=LOG_LEVEL.lower())


if __name__ == "__main__":
    main()
