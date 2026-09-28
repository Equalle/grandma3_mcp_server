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
INSTRUCTIONS = """grandMA3 reference docs (commands, Lua API, plugin/UI development, live-verified workflows).
Route: get_doc("index") first -- it maps tasks to topics. Then search_docs with 1-3 exact terms
(API names, keywords); each result names its best sections -- fetch one with get_doc(topic, section=...)
instead of the whole topic. get_docs(topics) reads several outlines at once.
Base answers on what the docs say; if they do not cover it, say so.
Writes: propose_doc_update / propose_topic_change draft only; apply_doc_update only for items the user approved.
Status in frontmatter: live-verified/verified > draft; treat draft as manual-derived and unverified."""

mcp = FastMCP("grandMA3 MCP Server", instructions=INSTRUCTIONS, host="0.0.0.0", port=8000, stateless_http=True)


# --- Documentation tools (read) ---

@mcp.tool()
def list_topics() -> list[dict]:
    """List all documentation topics with a one-line summary of what each covers."""
    return docs.list_topics()


@mcp.tool()
def get_doc(topic: str, full: bool = False, section: str = "") -> dict:
    """Return a topic's frontmatter, outline and summary by default; full=True for the complete markdown; section="<heading text>" (exact, prefix or substring, numbering optional) for just that section incl. its subsections."""
    return docs.get_doc(topic, full, section)


@mcp.tool()
def get_docs(topics: list[str], full: bool = False, section: str = "") -> dict:
    """Several topics in one call (max 8): outline+summary by default, full=True for content (capped at 60k chars total; the rest listed as omitted), or the same section of each."""
    return docs.get_docs(topics, full, section)


@mcp.tool()
def search_docs(query: str, limit: int = 5) -> dict:
    """Ranked search (BM25 over sections; whole words, identifier parts, plural-insensitive; title/heading/verified boosts). Use 1-3 exact terms. Each result lists its best-matching sections with excerpts -- fetch one with get_doc(topic, section=heading). limit max 15."""
    return docs.search_docs(query, limit)


@mcp.tool()
def lint_docs(topic: str = "") -> dict:
    """Read-only corpus check (all topics, or one): broken links/fences/index entries (error), missing frontmatter, references to non-existent topics, stale proposals (warn), placeholder summaries and very large topics (info)."""
    return docs.lint_docs(topic)


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


@mcp.tool()
def get_proposal(topic: str) -> dict:
    """One pending proposal in full: action, description, stale flag and the complete diff (up to 400 lines) -- for reviewing before apply."""
    return docs.get_proposal(topic)


# --- Documentation tools (write with approval) ---

@mcp.tool()
def propose_doc_update(topic: str, new_content: str, description: str = "", summary: str = "") -> dict:
    """Propose a doc update and return the diff for user review. description = what this edit changes; summary = optional one-line description of what the whole topic covers (kept in the index). Nothing changes until apply_doc_update."""
    return docs.propose_doc_update(topic, new_content, description, summary)


@mcp.tool()
def propose_topic_change(topic: str, action: str, new_topic: str = "", description: str = "") -> dict:
    """Draft deleting (action="delete") or renaming (action="rename", new_topic=...) a topic. Returns the topics that reference it. Nothing changes until apply_doc_update. Merge = propose_doc_update on the target + propose_topic_change delete on the source."""
    return docs.propose_topic_change(topic, action, new_topic, description)


@mcp.tool()
def apply_doc_update(topic: str, description: str = "") -> dict:
    """Apply a pending proposal the user approved (edit, new topic, delete or rename): archives the old version, writes the result, updates the index. Refused if the topic changed after the proposal was drafted."""
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
