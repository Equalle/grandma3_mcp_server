import hmac
import json
import logging
import os
from pathlib import Path

import uvicorn
from mcp.server.fastmcp import FastMCP, Image

from mcp_server.tools import docs, ui
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
Status in frontmatter: live-verified/verified > draft; treat draft as manual-derived and unverified.
Writes go through proposals: propose_section_update (one section -- preferred) / propose_doc_update
(whole topic or new topic) / propose_topic_change (delete, rename). A proposal is `untested` until live
evidence is attached (evidence= on propose, or confirm_proposal later: what ran, what came back, MA3
version) -> `verified`. apply_doc_update applies only verified, non-stale proposals that do not overlap
another open one and add no lint errors (unclosed fence, broken link; `lint` on propose shows them).
Readers see open proposals (pending_proposals) as unverified hints: confirm one you
verified live, dispute_proposal one that is wrong. Never attach evidence you did not observe.
UI look: ui_examples lists MA3's own dialogs worth copying (screenshot <-> .uixml, by pattern); ui_view shows
one with each widget's .uixml file:line; ui_find goes from something seen on screen to its source, ui_xml
from a source line to where it is seen; search_uixml / resolve_color / resolve_icon cover the UI source,
theme colours and icons."""

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
def list_proposals(topic: str = "") -> dict:
    """Open doc proposals (optionally one topic's): id, kind, section, status (untested/verified/disputed), stale, conflicts_with, applyable."""
    return docs.list_proposals(topic)


@mcp.tool()
def get_proposal(topic: str = "", id: str = "") -> dict:
    """One proposal in full (by id, or a topic's only open one): status, evidence entries, stale/conflicts, and the diff (up to 400 lines; a section proposal shows that section's diff)."""
    return docs.get_proposal(topic, id)


# --- Documentation tools (write: propose -> verify -> apply) ---

@mcp.tool()
def propose_section_update(topic: str, section: str, new_text: str, description: str = "", evidence: str = "",
                           by: str = "", ma3_version: str = "", replaces: str = "") -> dict:
    """Propose replacing ONE section (preferred for edits). section = heading to find (like get_doc(section=)); new_text = the full new section starting with its heading line (subsections included). evidence = what you ran live and what came back (makes it verified); without it the proposal is untested. by = who you are (agent/session label). replaces = id of your own earlier proposal this supersedes. Returns id, status, the section diff and any conflicts."""
    return docs.propose_section_update(topic, section, new_text, description, evidence, by, ma3_version, replaces)


@mcp.tool()
def propose_doc_update(topic: str, new_content: str, description: str = "", summary: str = "", evidence: str = "",
                       by: str = "", ma3_version: str = "", replaces: str = "") -> dict:
    """Propose a whole-topic update or a new topic (for edits prefer propose_section_update). description = what this edit changes; summary = optional one-line description of what the topic covers (kept in the index). evidence / by / ma3_version / replaces as in propose_section_update. Returns id, status and the diff."""
    return docs.propose_doc_update(topic, new_content, description, summary, evidence, by, ma3_version, replaces)


@mcp.tool()
def propose_topic_change(topic: str, action: str, new_topic: str = "", description: str = "", evidence: str = "",
                         by: str = "", replaces: str = "") -> dict:
    """Draft deleting (action="delete") or renaming (action="rename", new_topic=...) a topic. Returns the topics that reference it. Merge = propose_doc_update on the target + propose_topic_change delete on the source."""
    return docs.propose_topic_change(topic, action, new_topic, description, evidence, by, replaces)


@mcp.tool()
def confirm_proposal(id: str = "", evidence: str = "", by: str = "", ma3_version: str = "", topic: str = "") -> dict:
    """Attach live evidence that an open proposal is right (what ran, what came back) -> status verified. Only for evidence you observed yourself."""
    return docs.confirm_proposal(topic, id, evidence, by, ma3_version)


@mcp.tool()
def dispute_proposal(id: str = "", evidence: str = "", by: str = "", ma3_version: str = "", topic: str = "") -> dict:
    """Attach counter-evidence to an open proposal -> status disputed (never applied; resolve by reject + new proposal)."""
    return docs.dispute_proposal(topic, id, evidence, by, ma3_version)


@mcp.tool()
def apply_doc_update(id: str = "", description: str = "", topic: str = "") -> dict:
    """Apply one proposal (by id, or a topic's only open one). Refused unless it is verified, not stale (its topic/section unchanged since the draft) and not overlapping another open proposal. Archives the old version, updates the index, commits with the evidence."""
    return docs.apply_doc_update(topic, description, id)


@mcp.tool()
def reject_proposal(id: str = "", topic: str = "") -> dict:
    """Discard a proposal (by id, or a topic's only open one)."""
    return docs.reject_proposal(topic, id)


# --- UI reference: native MA3 screenshots <-> .uixml source, colours, icons ---

def _with_image(info: dict, png: bytes | None) -> list:
    return [info, Image(data=png, format="png")] if png else [info]


def _ui(fn, *args):
    try:
        return fn(*args)
    except FileNotFoundError as e:
        return {"error": str(e)}


def _ui_img(fn, *args) -> list:
    try:
        return _with_image(*fn(*args))
    except FileNotFoundError as e:
        return [{"error": str(e)}]


@mcp.tool()
def ui_examples(pattern: str = "", version: str = "") -> dict:
    """START HERE for building a plugin UI that looks native: the reviewed set of MA3's own menus/dialogs whose look the .uixml explains (one per layout), each with what it shows, its UI patterns and its .uixml file. pattern filters (tabs, property-form, toggles, swipe-buttons, buttons, text-input, radio, faders, colour-appearance, grid-table, scroll-list, auto-layout, main-dialog, resize-split, pool-tiles). version = MA3 version prefix (default: newest)."""
    return _ui(ui.ui_examples, pattern, version)


@mcp.tool()
def ui_find(query: str, limit: int = 10, kind: str = "", version: str = "") -> dict:
    """Find where something visible on the MA3 screen is: searches widget text, Name, class and menu name in the reference screenshots (all words must match). Each hit gives the menu, state, widget rect and its .uixml file:line (or the element that builds it at runtime), plus the ui_view call that shows it. kind = menu|window|settings|editor."""
    return _ui(ui.ui_find, query, limit, kind, version)


@mcp.tool(structured_output=False)
def ui_view(menu: str, state: str = "", widget: str = "", pad: int = 24, scale: float = 0.0, version: str = "") -> list:
    """Screenshot of a reference menu (a state = one of its tabs) with a legend: every labelled widget's rect IN THIS IMAGE and its .uixml file:line. widget = id, Name or Text crops to that widget (+pad px). scale shrinks the image (default: fit 1600 px)."""
    return _ui_img(ui.ui_view, menu, state, widget, pad, scale, version)


@mcp.tool(structured_output=False)
def ui_xml(file: str, line: int = 0, context: int = 10, show: bool = False, version: str = "") -> list:
    """Open a lib_menus .uixml (path relative to lib_menus, or a unique file name): without line = head + which lines appear on screen; with line = the element in context + where it is seen (menu, state, rect). show=True adds a crop of it from the screenshot."""
    return _ui_img(ui.ui_xml, file, line, context, show, version)


@mcp.tool()
def search_uixml(query: str, limit: int = 30, files: str = "uixml", version: str = "") -> dict:
    """Search MA3's own UI source (lib_menus): case-insensitive, all words on the same line; "re:<regex>" for a regex. files = uixml | lua | xml | all. Use it to find how MA3 writes an attribute, class or handler; ui_xml(file, line) then shows the element and where it appears on screen."""
    return _ui(ui.search_uixml, query, limit, files, version)


@mcp.tool(structured_output=False)
def resolve_color(token: str, limit: int = 40, version: str = "") -> list:
    """A colour token of the default theme (e.g. "Global.Transparent", "Button.Background"): its RGBA, the ColorDefRef chain and a swatch. A partial word lists matching tokens."""
    return _ui_img(ui.resolve_color, token, limit, version)


@mcp.tool(structured_output=False)
def resolve_icon(name: str, limit: int = 40, version: str = "") -> list:
    """A texture/icon name as used in Icon= / Texture= (graphics.textures.xml): kind, file, size and the rendered image. A partial word lists matching names."""
    return _ui_img(ui.resolve_icon, name, limit, version)


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
