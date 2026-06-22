# Installation Guide: grandMA3 MCP Server Add-on

## Prerequisites

- **Home Assistant OS** running on Raspberry Pi 4/5 or compatible (aarch64 / armv7)
- **grandMA3 onPC** running on your Windows/Mac (local machine, not on the Pi)
- **Tailscale** installed on both the Pi and your development machine (for remote access)
- **MA3-Terminal bridge** available on your machine (`C:\ProgramData\MALightingTechnology\gma3_library\datapools\plugins\MA3-Terminal\`)

## Step 1: Prepare the add-on directory

1. SSH into your Home Assistant machine or access the terminal via the HA web interface:
   ```bash
   ssh root@homeassistant.local
   # or use the HA web terminal (Settings → System → Terminal)
   ```

2. Navigate to the add-ons directory:
   ```bash
   cd /addons
   ```

3. Clone or copy this add-on directory into `/addons`:
   ```bash
   # Option A: Git clone
   git clone <your-repo-url> grandma3_mcp_server
   
   # Option B: SCP from your machine
   scp -r grandma3-mcp-addon root@homeassistant.local:/addons/grandma3_mcp_server
   
   # Option C: Manual — copy files one by one via SFTP
   ```

4. Verify the structure:
   ```bash
   ls -la /addons/grandma3_mcp_server/
   # Should show: Dockerfile, config.yaml, README.md, mcp_server/
   ```

## Step 2: Add to Home Assistant

1. **Go to Home Assistant web UI** → **Settings** → **Add-ons & Backups** → **Add-on Store** (bottom right)

2. Click the three-dot menu (⋯) → **Check for Updates**
   - This rescans the `/addons` directory

3. Look for **grandMA3 MCP Server** in the list
   - If not visible, try refreshing the page or restarting Home Assistant

4. Click **grandMA3 MCP Server** → **Install**
   - Home Assistant will build the Docker image from the Dockerfile (takes 2–5 min on Pi)

## Step 3: Configure the add-on

1. Once installed, click **Configuration**

2. Set the options:
   ```yaml
   ma3_host: "192.168.1.100"    # (not used for docs-only, but keep it for future)
   ma3_port: 30000               # (not used for docs-only)
   log_level: "info"             # or "debug" for verbose output
   ```

3. Click **Save**

## Step 4: Start the add-on

1. Click **Start**
2. Wait for the "Started" message
3. Check the **Logs** tab to confirm:
   ```
   INFO:     Uvicorn running on http://0.0.0.0:8000
   ```

## Step 5: Create the documentation directory

1. SSH back into the Pi:
   ```bash
   ssh root@homeassistant.local
   ```

2. Create the docs directory:
   ```bash
   mkdir -p /config/mcp_docs
   cd /config/mcp_docs
   ```

3. **Option A: Start with the example index**
   ```bash
   # Copy the example from the add-on
   cp /addons/grandma3_mcp_server/mcp_server/docs/index.json.example ./index.json
   ```

4. **Option B: Start fresh** (Claude will auto-scan .md files)
   ```bash
   # Just create the directory; let Claude create docs as needed
   touch index.json  # empty for now
   ```

5. **Add your first docs** (optional):
   ```bash
   cat > getting-started.md << 'EOF'
   # Getting Started
   
   This is the grandMA3 documentation hub. Claude can read and update these docs.
   
   - Store architecture details here
   - Lua API patterns
   - Fixture mappings
   - Club-switching workflows
   
   When Claude finds new info, it proposes updates with diffs for your review.
   EOF
   ```

6. **Create subdirectories** if using the example index:
   ```bash
   mkdir -p architecture lua-api
   
   # Example files (can be empty initially)
   touch architecture/fixture-id-ranges.md
   touch architecture/datapool-layout.md
   touch lua-api/overview.md
   ```

## Step 6: Verify the add-on is working

1. In HA web UI, go to **Settings** → **Add-ons & Backups** → **grandMA3 MCP Server**

2. Click **Open Web UI** (if available) or manually test via curl:
   ```bash
   # From the Pi terminal
   curl -X POST http://localhost:8000/mcp \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
   ```
   - Should return a JSON-RPC response listing the available tools

3. Check the **Logs** tab for any errors

## Step 7: Connect from Claude Code

### Find your Pi's Tailscale IP

1. SSH into the Pi:
   ```bash
   tailscale status
   ```
   - Look for a line like: `100.x.x.x  homeassistant           ...`
   - This is your Pi's Tailscale IP

2. Or from HA web UI: **Settings** → **System** → **About** (Tailscale info sometimes shown)

### Add the MCP server in Claude Code

1. **Open Claude Code** on your machine
2. Go to **Settings** (gear icon) → **MCP Servers** or **Claude > Preferences > MCP**
3. Click **Add Server**
4. Configure:
   ```
   Name:     grandMA3 Docs (or whatever you like)
   Type:     HTTP (streamable HTTP)
   URL:      http://100.x.x.x:8000/mcp
   (replace 100.x.x.x with your Pi's Tailscale IP)
   ```
5. Click **Add** / **Save**
6. Claude Code should connect and show the tools/prompts available

### Test it

1. In Claude Code, ask:
   ```
   What documentation topics are available?
   ```
   - Claude should call `list_topics()` and show the docs from your Pi

2. Ask Claude to add a new doc:
   ```
   Can you create a doc about our fixture ID ranges?
   Please propose: fixture IDs 1-100 are movers, 101-200 are LEDs, 201-300 are conventionals.
   ```
   - Claude calls `propose_doc_update()` and shows you the diff
   - You review and confirm
   - Claude calls `apply_doc_update()` to apply it
   - Old version is automatically archived

## Step 8: Set up MA3-Terminal on your machine

Each time you want to use the system with a show running locally:

### Windows
```powershell
# Open PowerShell
Import-Module 'C:\ProgramData\MALightingTechnology\gma3_library\datapools\plugins\MA3-Terminal\MA3Terminal.psm1'

# Start the bridge
Start-MA3Session

# Verify it's working
ma3 "list"
```

### Mac
```bash
# PowerShell is available on macOS
pwsh

# Import the module (adjust path if needed)
Import-Module '/path/to/MA3-Terminal/MA3Terminal.psm1'

# Start the bridge
Start-MA3Session

# Test
ma3 "list"
```

Now Claude Code can access both:
- **Docs from the Pi** (via Tailscale MCP)
- **Show control** (via local MA3-Terminal bridge)

## Troubleshooting

### "Add-on failed to build"
- Check the **Logs** tab for errors
- Ensure you have enough disk space on the Pi (at least 1GB free)
- Try restarting Home Assistant and retrying

### "MCP connection refused"
- Verify Tailscale is running on both Pi and your machine
- Check the Tailscale IP: `tailscale status` on the Pi
- Make sure your firewall allows port 8000
- Test: `curl http://100.x.x.x:8000/mcp` from your machine

### "Documentation directory not found"
- SSH into the Pi and verify `/config/mcp_docs/` exists
- Create it if missing: `mkdir -p /config/mcp_docs`
- Make sure the add-on has write permissions: `chmod 777 /config/mcp_docs`

### "Claude can't find docs"
- Ensure at least one `.md` file or an `index.json` exists in `/config/mcp_docs/`
- Check the add-on **Logs** for errors
- Restart the add-on: **Settings** → **Add-ons & Backups** → **Stop** then **Start**

### "Proposals not applying"
- Verify `/config/mcp_docs/` is writable by the container user
- Check the add-on **Logs** for permission errors
- Try: `sudo chown 1000:1000 /config/mcp_docs` on the Pi

## Next steps

- **Add documentation** by manually creating `.md` files in `/config/mcp_docs/`
- **Use Claude to improve docs** — propose updates, archive old versions
- **Integrate MA3-Terminal** on your local machine to add show control
- **Set up Tailscale access** from multiple devices so you can work from anywhere

## Support

For issues:
1. Check the **Logs** in the HA web UI
2. Verify the docs directory exists and is writable
3. Test connectivity: `curl http://100.x.x.x:8000/mcp` from your machine
4. Review the README in the add-on for architecture details

claude mcp remove grandma3
claude mcp add --transport http grandma3 http://100.106.68.4:8000/mcp