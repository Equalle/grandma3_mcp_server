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


LIST_LIMIT = 50


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


def _capped_list(items: list[str]) -> dict:
    """Cap a list response and report how many entries were omitted."""
    return {"items": items[:LIST_LIMIT], "total": len(items)}


def list_topics() -> list[dict]:
    """List all indexed topics from index.json, or auto-scan .md files."""
    index = _index()
    if index:
        topics = []
        for k, v in index.items():
            topic = {"topic": k, "description": v.get("description", "")}
            if v.get("updated"):
                topic["updated"] = v["updated"]
            topics.append(topic)
        return topics

    topics = []
    for md in sorted(DOCS_DIR.rglob("*.md")):
        if ".archive" in md.parts or ".proposals" in md.parts:
            continue
        rel = md.relative_to(DOCS_DIR)
        topics.append({"topic": str(rel.with_suffix("")), "description": ""})
    return topics


SUMMARY_CHAR_LIMIT = 300


def get_doc(topic: str, full: bool = False) -> dict:
    """Return a doc's content. By default returns a cheap outline+summary; pass full=True for the entire markdown."""
    index = _index()
    if topic in index:
        file_path = DOCS_DIR / index[topic].get("file", f"{topic}.md")
    else:
        file_path = DOCS_DIR / f"{topic}.md"

    if not file_path.exists():
        return {"topic": topic, "error": f"Topic '{topic}' not found."}

    text = file_path.read_text()
    if full:
        return {"topic": topic, "content": text}

    outline = [line.strip() for line in text.splitlines() if line.strip().startswith("#")]
    body_lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    summary = " ".join(body_lines).strip()
    if len(summary) > SUMMARY_CHAR_LIMIT:
        summary = summary[:SUMMARY_CHAR_LIMIT] + "…"

    return {
        "topic": topic,
        "outline": outline,
        "summary": summary,
        "length_chars": len(text),
        "hint": "call get_doc(topic, full=True) for the complete content",
    }


SEARCH_RESULT_LIMIT = 5
EXCERPT_CHAR_LIMIT = 150


def search_docs(query: str) -> dict:
    """Tokenized search across all .md files; ranks topics by number of matched query words."""
    words = sorted(set(w.lower() for w in re.findall(r"\w+", query) if len(w) > 1))
    if not words:
        return {"results": [], "total_matches": 0}

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
                excerpt = " ".join(lines[start:end]).strip()
                if len(excerpt) > EXCERPT_CHAR_LIMIT:
                    excerpt = excerpt[:EXCERPT_CHAR_LIMIT] + "…"
                excerpts.append(excerpt)
                if len(excerpts) >= 3:
                    break

        results.append({"topic": rel, "matched_terms": matched, "excerpts": excerpts})

    results.sort(key=lambda r: len(r["matched_terms"]), reverse=True)
    total_matches = len(results)
    return {"results": results[:SEARCH_RESULT_LIMIT], "total_matches": total_matches}


def list_files() -> dict:
    """Raw file listing of the docs directory, capped to the first 50 entries."""
    if not DOCS_DIR.exists():
        return _capped_list([])
    files = [str(p.relative_to(DOCS_DIR)) for p in sorted(DOCS_DIR.rglob("*"))
             if p.is_file() and ".archive" not in p.parts and ".proposals" not in p.parts]
    return _capped_list(files)


DIFF_LINE_LIMIT = 100
NEW_DOC_PREVIEW_CHAR_LIMIT = 300


def propose_doc_update(topic: str, new_content: str, description: str = "") -> dict:
    """Propose a doc update; returns diff for user review. User must approve before applying."""
    _ensure_dirs()

    index = _index()
    filename = index.get(topic, {}).get("file", f"{topic}.md")
    file_path = DOCS_DIR / filename

    is_new_doc = not file_path.exists()
    old_content = file_path.read_text() if not is_new_doc else "(new document)"

    proposal_file = PROPOSALS_DIR / f"{topic}.proposal.md"
    proposal_file.parent.mkdir(parents=True, exist_ok=True)
    proposal_data = {
        "topic": topic,
        "description": description,
        "proposed_at": datetime.now().isoformat(),
        "new_content": new_content,
    }
    proposal_file.write_text(json.dumps(proposal_data, indent=2))

    result = {"topic": topic, "status": "pending_review", "proposal_id": topic}

    if is_new_doc:
        preview = new_content[:NEW_DOC_PREVIEW_CHAR_LIMIT]
        if len(new_content) > NEW_DOC_PREVIEW_CHAR_LIMIT:
            preview += "…"
        result.update({"diff": None, "new_doc_preview": preview, "new_doc_lines": len(new_content.splitlines())})
        return result

    if old_content == new_content:
        result["diff"] = "(no changes)"
        return result

    import difflib
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(old_lines, new_lines, fromfile=f"{topic} (current)", tofile=f"{topic} (proposed)"))

    if len(diff_lines) > DIFF_LINE_LIMIT:
        result["diff"] = "".join(diff_lines[:DIFF_LINE_LIMIT])
        result["diff_truncated"] = True
        result["diff_total_lines"] = len(diff_lines)
    else:
        result["diff"] = "".join(diff_lines)

    return result


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


def list_archives() -> dict:
    """List all archived document versions, capped to the first 50 entries."""
    _ensure_dirs()
    if not ARCHIVE_DIR.exists():
        return _capped_list([])
    archives = [str(p.relative_to(ARCHIVE_DIR)) for p in sorted(ARCHIVE_DIR.glob("*.md"))]
    return _capped_list(archives)
