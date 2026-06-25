import json
import os
import re
from datetime import datetime
from pathlib import Path

DOCS_DIR = Path("/config/mcp_docs")
PROPOSALS_DIR = DOCS_DIR / ".proposals"
ARCHIVE_DIR = DOCS_DIR / ".archive"


def _ensure_dirs():
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    PROPOSALS_DIR.mkdir(exist_ok=True)
    ARCHIVE_DIR.mkdir(exist_ok=True)


def _index() -> dict:
    index_file = DOCS_DIR / "index.json"
    if index_file.exists():
        return json.loads(index_file.read_text())
    return {}


def _update_index(topic: str, description: str = "", filename: str = None):
    """Update index.json with a new or existing topic."""
    _ensure_dirs()
    index = _index()
    if filename is None:
        filename = f"{topic}.md"
    index[topic] = {
        "description": description,
        "file": filename,
        "updated": datetime.now().isoformat(),
    }
    (DOCS_DIR / "index.json").write_text(json.dumps(index, indent=2))


def list_topics() -> list[dict]:
    """List all indexed topics from index.json, or auto-scan .md files."""
    index = _index()
    if index:
        return [{"topic": k, "description": v.get("description", ""), "updated": v.get("updated", "")} for k, v in index.items()]

    topics = []
    for md in sorted(DOCS_DIR.rglob("*.md")):
        if ".archive" in md.parts or ".proposals" in md.parts:
            continue
        rel = md.relative_to(DOCS_DIR)
        topics.append({"topic": str(rel.with_suffix("")), "description": ""})
    return topics


def get_doc(topic: str) -> str:
    """Return full markdown content for a topic."""
    index = _index()
    if topic in index:
        file_path = DOCS_DIR / index[topic].get("file", f"{topic}.md")
    else:
        file_path = DOCS_DIR / f"{topic}.md"

    if not file_path.exists():
        return f"Topic '{topic}' not found."
    return file_path.read_text()


def search_docs(query: str) -> list[dict]:
    """Tokenized search across all .md files; ranks topics by number of matched query words."""
    words = sorted(set(w.lower() for w in re.findall(r"\w+", query) if len(w) > 1))
    if not words:
        return []

    results = []
    for md in sorted(DOCS_DIR.rglob("*.md")):
        if ".archive" in md.parts or ".proposals" in md.parts:
            continue
        text = md.read_text()
        text_lower = text.lower()
        matched = [w for w in words if w in text_lower]
        if not matched:
            continue

        rel = str(md.relative_to(DOCS_DIR).with_suffix(""))
        lines = text.splitlines()
        excerpts = []
        for i, line in enumerate(lines):
            line_lower = line.lower()
            if any(w in line_lower for w in matched):
                start = max(0, i - 1)
                end = min(len(lines), i + 2)
                excerpts.append(" ".join(lines[start:end]).strip())
                if len(excerpts) >= 3:
                    break

        results.append({"topic": rel, "matched_terms": matched, "excerpts": excerpts})

    results.sort(key=lambda r: len(r["matched_terms"]), reverse=True)
    return results


def list_files() -> list[str]:
    """Raw file listing of the docs directory."""
    if not DOCS_DIR.exists():
        return []
    return [str(p.relative_to(DOCS_DIR)) for p in sorted(DOCS_DIR.rglob("*"))
            if p.is_file() and ".archive" not in p.parts and ".proposals" not in p.parts]


def propose_doc_update(topic: str, new_content: str, description: str = "") -> dict:
    """Propose a doc update; returns diff for user review. User must approve before applying."""
    _ensure_dirs()

    index = _index()
    filename = index.get(topic, {}).get("file", f"{topic}.md")
    file_path = DOCS_DIR / filename

    old_content = file_path.read_text() if file_path.exists() else "(new document)"

    old_lines = old_content.splitlines(keepends=True) if old_content != "(new document)" else []
    new_lines = new_content.splitlines(keepends=True)

    import difflib
    diff = "".join(difflib.unified_diff(old_lines, new_lines, fromfile=f"{topic} (current)", tofile=f"{topic} (proposed)"))

    proposal_file = PROPOSALS_DIR / f"{topic}.proposal.md"
    proposal_file.parent.mkdir(parents=True, exist_ok=True)
    proposal_data = {
        "topic": topic,
        "description": description,
        "proposed_at": datetime.now().isoformat(),
        "new_content": new_content,
    }
    proposal_file.write_text(json.dumps(proposal_data, indent=2))

    return {
        "topic": topic,
        "status": "pending_review",
        "diff": diff if diff else "(no changes)" if old_content == new_content else f"New document:\n\n{new_content[:500]}...",
        "proposal_id": topic,
    }


def apply_doc_update(topic: str, description: str = "") -> dict:
    """Apply a proposed doc update: archive old version, write new, update index."""
    _ensure_dirs()

    proposal_file = PROPOSALS_DIR / f"{topic}.proposal.md"
    if not proposal_file.exists():
        return {"status": "error", "message": f"No proposal found for '{topic}'. Use propose_doc_update first."}

    proposal_data = json.loads(proposal_file.read_text())
    new_content = proposal_data["new_content"]

    index = _index()
    filename = index.get(topic, {}).get("file", f"{topic}.md")
    file_path = DOCS_DIR / filename

    if file_path.exists():
        old_content = file_path.read_text()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_name = f"{topic}-{timestamp}.md"
        archive_file = ARCHIVE_DIR / archive_name
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        archive_file.write_text(old_content)

    suffix = f"\n\n---\n*Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}. [See archive for previous versions](./.archive/)*"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(new_content + suffix)

    _update_index(topic, description or proposal_data.get("description", ""), filename)
    proposal_file.unlink()

    return {
        "status": "applied",
        "topic": topic,
        "file": str(file_path.relative_to(DOCS_DIR)),
        "message": "Doc updated and archived successfully.",
    }


def reject_proposal(topic: str) -> dict:
    """Reject a proposed update."""
    proposal_file = PROPOSALS_DIR / f"{topic}.proposal.md"
    if proposal_file.exists():
        proposal_file.unlink()
        return {"status": "rejected", "topic": topic}
    return {"status": "error", "message": f"No proposal found for '{topic}'."}


def list_archives() -> list[str]:
    """List all archived document versions."""
    _ensure_dirs()
    if not ARCHIVE_DIR.exists():
        return []
    return [str(p.relative_to(ARCHIVE_DIR)) for p in sorted(ARCHIVE_DIR.glob("*.md"))]
