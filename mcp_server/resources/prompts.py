from pathlib import Path

ARCH_DIR = Path("/config/mcp_docs/architecture")


def _load_architecture() -> str:
    if not ARCH_DIR.exists():
        return "(architecture docs not yet loaded — place .md files in /config/mcp_docs/architecture/)"
    parts = []
    for md in sorted(ARCH_DIR.glob("*.md")):
        parts.append(f"## {md.stem}\n\n{md.read_text()}")
    return "\n\n".join(parts) if parts else "(no architecture docs found)"


GRANDMA3_EXPERT = """\
You are an expert grandMA3 lighting console operator and Lua plugin developer.
You reason in terms of datapools, executors, sequences, presets, MAtricks, and the MA3 Lua API.
You are familiar with the following show file architecture:

{architecture}
"""


def grandma3_expert() -> str:
    return GRANDMA3_EXPERT.format(architecture=_load_architecture())
