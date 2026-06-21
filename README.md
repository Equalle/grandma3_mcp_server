# grandMA3 MCP Server — Home Assistant Add-on

A Home Assistant OS local add-on that runs an MCP (Model Context Protocol) server for collaborative grandMA3 documentation and show management.

## Architecture

```
                Your Machine (Windows/Mac)              Pi (Home Assistant)
                ===========================              ====================
                
     grandMA3 ─── MA3-Terminal Bridge ───┐         MCP Server
     (console)    (local PowerShell IPC) │         (SSE over Tailscale)
                                         │
                                         └─── Claude Code ←──→ Docs + Archive
                                              (local + remote)
```

**Key principle:** Single source of truth on the Pi for documentation. Show control stays local on your machine.

### What the Pi does
- **Serves documentation** via MCP over Tailscale (read-only by default)
- **Stores proposed doc updates** with diff review (user must confirm before applying)
- **Archives old versions** when docs are updated (with redirect in current doc)
- **Auto-updates `index.json`** when new topics are created

### What your machine does
- **Runs MA3-Terminal bridge** locally (PowerShell, already provided)
- **Provides show control** to Claude via local IPC (low latency, no network dependency)
- **Connects to Pi via Tailscale** to access shared documentation

### Workflow
1. **At home with Windows MA3:**
   - Start MA3-Terminal: `Start-MA3Session` (PowerShell, local)
   - Claude Code reads docs from Pi, controls MA3 locally
   
2. **At cafe with Mac MA3:**
   - Start MA3-Terminal on Mac
   - Connect to same Pi docs, different show control
   - All improvements to docs sync automatically

3. **Discover something new?**
   - Claude proposes: `propose_doc_update("topic", "new content")`
   - You review the diff
   - Approve: `apply_doc_update("topic")` 
   - Old version archived, new doc links to it

## Files

```
grandma3-mcp-addon/
├── config.yaml                  # HA add-on manifest
├── Dockerfile                   # Alpine + Python 3.11
├── README.md                    # This file
└── mcp_server/
    ├── main.py                  # SSE server, tool/prompt registration
    ├── requirements.txt         # mcp[cli], uvicorn, starlette
    ├── __init__.py
    ├── tools/
    │   ├── __init__.py
    │   └── docs.py              # Read/write docs with archive + index sync
    ├── resources/
    │   ├── __init__.py
    │   └── prompts.py           # MCP prompt resources (expert personas)
    └── docs/
        └── index.json.example   # Template for /config/mcp_docs/index.json
```

## MCP Tools

### Read documentation
- `list_topics()` — all indexed topics with descriptions
- `get_doc(topic)` — full markdown for a topic
- `search_docs(query)` — full-text search with excerpts
- `list_files()` — all doc files
- `list_archives()` — all archived versions

### Propose & apply updates
- `propose_doc_update(topic, new_content, description)` — shows diff, waits for approval
- `apply_doc_update(topic, description)` — archives old version, writes new, updates index
- `reject_proposal(topic)` — discard a proposal

### MCP Prompts
- `grandma3_expert` — expert operator persona (injects architecture docs at runtime)
- `club_switcher_assistant` — specialist for Lua club-switching plugin

## Setup

### On the Pi (Home Assistant)
1. Copy this directory to your HA add-ons folder
2. Go to Settings → Add-ons → Create Local Add-on, select this folder
3. Install and start the add-on
4. MCP server runs on `http://0.0.0.0:8000/sse`
5. Copy your `.md` docs to `/config/mcp_docs/` (or create an `index.json` there)

### On your machine (Windows/Mac)
```powershell
# Import the MA3-Terminal module (do this each session)
Import-Module 'C:\ProgramData\MALightingTechnology\gma3_library\datapools\plugins\MA3-Terminal\MA3Terminal.psm1'

# Start the bridge (once per MA3 session)
Start-MA3Session

# Test it
ma3 "list"
```

Then in Claude Code, connect to the Pi via Tailscale:
```
MCP settings → add server
Type: SSE
URL: http://<pi-tailscale-ip>:8000/sse
```

## Documentation directory structure

Place your docs in `/config/mcp_docs/` on the Pi. Recommended layout:

```
/config/mcp_docs/
├── index.json                              # Topics registry (auto-created)
├── getting-started.md
├── architecture/
│   ├── fixture-id-ranges.md
│   ├── datapool-layout.md
│   ├── group-numbering.md
│   └── preset-organization.md
├── lua-api/
│   ├── overview.md
│   ├── club-switcher-reference.md
│   └── common-patterns.md
└── .archive/                               # Auto-created; old versions
    ├── fixture-id-ranges-20260621-145600.md
    └── ...
```

When Claude updates a doc, the old version moves to `.archive/<topic>-<timestamp>.md`, and the new version includes a link back.

## Networking

- **MCP endpoint:** `http://0.0.0.0:8000/sse` (binds inside container)
- **Access from Claude:** via Tailscale at `http://100.x.x.x:8000/sse` (replace with your Pi's Tailscale IP)
- **Show control:** local MA3-Terminal bridge on your machine (no network hop)

## Constraints & notes

- Docs directory is read/written on every request — no caching, no restart needed to update
- All MCP tools fail gracefully if docs directory is inaccessible
- Archive and `.proposals` directories are auto-created
- User must confirm doc updates via the Claude Code conversation (no auto-apply)
- Proposals are stored as temp JSON; rejected proposals are deleted
- Multi-user updates to the same doc could race; not a concern for personal setups
