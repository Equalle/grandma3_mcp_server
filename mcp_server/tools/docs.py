"""Documentation store: markdown topics in DOCS_DIR, an index.json, drafts and archives.

Safety properties (v1.2):
  * Topic names are validated and every path is resolved INSIDE DOCS_DIR, so a
    crafted topic cannot read or write elsewhere in the (rw-mapped) HA config.
  * All writes are atomic (temp file + os.replace) and serialised by one lock.
  * A proposal records the sha256 of the file it was drafted against; applying
    it after the file changed is refused instead of silently reverting the
    newer edit.
  * The index keeps a stable per-topic `summary` (what the topic covers) apart
    from `last_change` (what the last edit did) -- list_topics used to show the
    last change note as the description.
  * No footer is injected into content any more; `updated` lives in the index.
  * If DOCS_DIR is a git repository, every apply/reject is committed.
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

DOCS_DIR = Path(os.environ.get("MCP_DOCS_DIR", "/config/mcp_docs"))
PROPOSALS_DIR = DOCS_DIR / ".proposals"
ARCHIVE_DIR = DOCS_DIR / ".archive"
HIDDEN_PARTS = {".archive", ".proposals", ".git"}

LIST_LIMIT = 50
SUMMARY_CHAR_LIMIT = 300
TOPIC_SUMMARY_LIMIT = 200
SEARCH_RESULT_LIMIT = 5
EXCERPT_CHAR_LIMIT = 150
DIFF_LINE_LIMIT = 100
NEW_DOC_PREVIEW_CHAR_LIMIT = 300

_LOCK = threading.RLock()

# Lowercase path segments of [a-z0-9._-], each starting alphanumeric: no "..",
# no hidden directories, no absolute paths, no backslashes.
_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*$")
_ARCHIVE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*\.md$")

# The footer older versions appended on every apply (sometimes stacked).
_FOOTER_RE = re.compile(
    r"(\n*---\n\*Last updated: [^\n]*\[See archive for previous versions\]\(\./\.archive/\)\*\s*)+\Z")


class DocError(ValueError):
    pass


# --------------------------------------------------------------------------- paths

def _ensure_dirs():
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    PROPOSALS_DIR.mkdir(exist_ok=True)
    ARCHIVE_DIR.mkdir(exist_ok=True)


def _inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if resolved != root.resolve() and root.resolve() not in resolved.parents:
        raise DocError("path escapes the docs directory")
    return resolved


def _check_topic(topic: str) -> str:
    if not isinstance(topic, str) or not _TOPIC_RE.match(topic) or len(topic) > 120:
        raise DocError(f"Invalid topic name {topic!r}: use lowercase letters, digits, '-', '_', '.', "
                       "optionally '/'-separated, each part starting with a letter or digit.")
    return topic


def _topic_file(topic: str, index: dict | None = None) -> Path:
    _check_topic(topic)
    index = _index() if index is None else index
    rel = index.get(topic, {}).get("file", f"{topic}.md")
    path = _inside(DOCS_DIR / rel, DOCS_DIR)
    if HIDDEN_PARTS.intersection(path.relative_to(DOCS_DIR.resolve()).parts):
        raise DocError("topic file may not live in a hidden directory")
    return path


def _proposal_file(topic: str) -> Path:
    _check_topic(topic)
    return _inside(PROPOSALS_DIR / f"{topic}.proposal.md", PROPOSALS_DIR)


def _visible_md():
    for md in sorted(DOCS_DIR.rglob("*.md")):
        if HIDDEN_PARTS.intersection(md.relative_to(DOCS_DIR).parts):
            continue
        yield md


# --------------------------------------------------------------------------- io

def _atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _sha(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git_commit(message: str):
    """Commit everything in DOCS_DIR if it is a git repo. Never raises."""
    if not (DOCS_DIR / ".git").exists():
        return
    try:
        git = ["git", "-C", str(DOCS_DIR), "-c", "user.name=grandma3-mcp",
               "-c", "user.email=grandma3-mcp@localhost", "-c", "safe.directory=*"]
        subprocess.run(git + ["add", "-A"], check=True, capture_output=True, timeout=30)
        r = subprocess.run(git + ["commit", "-q", "-m", message], capture_output=True, timeout=30)
        if r.returncode not in (0, 1):   # 1 = nothing to commit
            log.warning("git commit failed: %s", r.stderr.decode(errors="replace").strip())
    except Exception as exc:  # noqa: BLE001 -- the commit must never break a tool call
        log.warning("git commit skipped: %s", exc)


# --------------------------------------------------------------------------- frontmatter

def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Very small YAML-ish frontmatter reader: top-level `key: value` lines only."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m:
            meta[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    body = text[end + 4:].lstrip("\n")
    return meta, body


def _derive_summary(text: str) -> str:
    meta, body = _split_frontmatter(text)
    for key in ("summary", "description", "title"):
        if meta.get(key):
            return meta[key][:TOPIC_SUMMARY_LIMIT]
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:TOPIC_SUMMARY_LIMIT]
    return ""


def _outline(body: str) -> list[str]:
    out, fenced = [], False
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("```"):
            fenced = not fenced
            continue
        if not fenced and re.match(r"^#{1,6} ", s):
            out.append(s)
    return out


# --------------------------------------------------------------------------- index

def _index() -> dict:
    index_file = DOCS_DIR / "index.json"
    if not index_file.exists():
        return {}
    return json.loads(_read(index_file))


def _write_index(index: dict):
    _atomic_write(DOCS_DIR / "index.json", json.dumps(index, indent=2, ensure_ascii=False) + "\n")


def reconcile_index() -> dict:
    """Startup check: migrate old entries, drop entries without a file, index stray files."""
    with _LOCK:
        _ensure_dirs()
        index = _index()
        dropped, added, migrated = [], [], 0
        for topic in list(index):
            entry = index[topic]
            try:
                path = _topic_file(topic, index)
                exists = path.exists()
            except DocError:
                exists = False
            if not exists:
                dropped.append(topic)
                del index[topic]
                continue
            if "last_change" not in entry:
                entry["last_change"] = entry.pop("description", "")
                migrated += 1
            if not entry.get("summary"):
                entry["summary"] = _derive_summary(_read(path))
        indexed_files = {e.get("file") for e in index.values()}
        for md in _visible_md():
            rel = str(md.relative_to(DOCS_DIR)).replace("\\", "/")
            topic = rel[:-3]
            if rel in indexed_files or not _TOPIC_RE.match(topic):
                continue
            index[topic] = {"summary": _derive_summary(_read(md)), "last_change": "", "file": rel,
                            "updated": datetime.fromtimestamp(md.stat().st_mtime).isoformat()}
            added.append(topic)
        if dropped or added or migrated:
            _write_index(index)
            _git_commit(f"Reconcile index: dropped {dropped}, added {added}, migrated {migrated}")
        result = {"dropped": dropped, "added": added, "migrated": migrated}
        log.info("index reconcile: %s", result)
        return result


def _capped(items: list, offset: int = 0) -> dict:
    offset = max(0, int(offset or 0))
    return {"items": items[offset:offset + LIST_LIMIT], "total": len(items), "offset": offset}


# --------------------------------------------------------------------------- read tools

def list_topics() -> list[dict]:
    """All topics with a stable one-line summary (what the topic covers)."""
    return [{"topic": k, "summary": v.get("summary", ""), "updated": v.get("updated", "")}
            for k, v in _index().items()]


def get_doc(topic: str, full: bool = False) -> dict:
    try:
        path = _topic_file(topic)
    except DocError as exc:
        return {"topic": topic, "error": str(exc)}
    if not path.exists():
        return {"topic": topic, "error": f"Topic '{topic}' not found."}
    text = _read(path)
    if full:
        return {"topic": topic, "content": text}
    meta, body = _split_frontmatter(text)
    plain = [s.strip() for s in body.splitlines() if s.strip() and not s.strip().startswith("#")]
    summary = " ".join(plain)
    if len(summary) > SUMMARY_CHAR_LIMIT:
        summary = summary[:SUMMARY_CHAR_LIMIT] + "…"
    return {
        "topic": topic,
        "frontmatter": meta,
        "outline": _outline(body),
        "summary": summary,
        "length_chars": len(text),
        "hint": "call get_doc(topic, full=True) for the complete content",
    }


def search_docs(query: str) -> dict:
    """Tokenized search across all topics; ranks by number of matched query words."""
    words = sorted(set(w.lower() for w in re.findall(r"\w+", query or "") if len(w) > 1))
    if not words:
        return {"results": [], "total_matches": 0}
    results = []
    for md in _visible_md():
        text = _read(md)
        text_lower = text.lower()
        matched = [w for w in words if w in text_lower]
        if not matched:
            continue
        lines = text.splitlines()
        excerpts = []
        for i, line in enumerate(lines):
            if any(w in line.lower() for w in matched):
                excerpt = " ".join(lines[max(0, i - 1):min(len(lines), i + 2)]).strip()
                if len(excerpt) > EXCERPT_CHAR_LIMIT:
                    excerpt = excerpt[:EXCERPT_CHAR_LIMIT] + "…"
                excerpts.append(excerpt)
                if len(excerpts) >= 3:
                    break
        rel = str(md.relative_to(DOCS_DIR).with_suffix("")).replace("\\", "/")
        results.append({"topic": rel, "matched_terms": matched, "excerpts": excerpts})
    results.sort(key=lambda r: len(r["matched_terms"]), reverse=True)
    return {"results": results[:SEARCH_RESULT_LIMIT], "total_matches": len(results)}


def list_files(offset: int = 0) -> dict:
    if not DOCS_DIR.exists():
        return _capped([])
    files = [str(p.relative_to(DOCS_DIR)).replace("\\", "/") for p in sorted(DOCS_DIR.rglob("*"))
             if p.is_file() and not HIDDEN_PARTS.intersection(p.relative_to(DOCS_DIR).parts)]
    return _capped(files, offset)


def list_archives(topic: str = "", offset: int = 0) -> dict:
    """Archived versions, newest first; optionally only one topic's."""
    _ensure_dirs()
    items = []
    for p in ARCHIVE_DIR.rglob("*.md"):
        rel = str(p.relative_to(ARCHIVE_DIR)).replace("\\", "/")
        m = re.match(r"^(.*)-(\d{8}-\d{6})\.md$", rel)
        if topic and (not m or m.group(1) != topic):
            continue
        items.append((m.group(2) if m else "", rel))
    items.sort(reverse=True)
    return _capped([rel for _, rel in items], offset)


def get_archive(name: str) -> dict:
    """Content of one archived version (name as returned by list_archives)."""
    if not isinstance(name, str) or not _ARCHIVE_RE.match(name) or ".." in name:
        return {"error": f"Invalid archive name {name!r}."}
    try:
        path = _inside(ARCHIVE_DIR / name, ARCHIVE_DIR)
    except DocError as exc:
        return {"error": str(exc)}
    if not path.exists():
        return {"error": f"Archive '{name}' not found."}
    return {"name": name, "content": _read(path)}


# --------------------------------------------------------------------------- write tools

def propose_doc_update(topic: str, new_content: str, description: str = "", summary: str = "") -> dict:
    """Draft an update and return the diff. Nothing changes until apply_doc_update."""
    with _LOCK:
        try:
            path = _topic_file(topic)
            proposal_file = _proposal_file(topic)
        except DocError as exc:
            return {"topic": topic, "status": "error", "message": str(exc)}
        _ensure_dirs()
        old = _read(path) if path.exists() else None
        replaced_pending = proposal_file.exists()
        _atomic_write(proposal_file, json.dumps({
            "topic": topic,
            "description": description,
            "summary": summary,
            "proposed_at": datetime.now().isoformat(),
            "base_sha256": _sha(old),
            "new_content": new_content,
        }, indent=2, ensure_ascii=False))

    result = {"topic": topic, "status": "pending_review", "proposal_id": topic}
    if replaced_pending:
        result["replaced_pending_proposal"] = True
    if old is None:
        preview = new_content[:NEW_DOC_PREVIEW_CHAR_LIMIT]
        if len(new_content) > NEW_DOC_PREVIEW_CHAR_LIMIT:
            preview += "…"
        result.update({"diff": None, "new_doc_preview": preview, "new_doc_lines": len(new_content.splitlines())})
        return result
    if old == new_content:
        result["diff"] = "(no changes)"
        return result
    import difflib
    diff = list(difflib.unified_diff(old.splitlines(keepends=True), new_content.splitlines(keepends=True),
                                     fromfile=f"{topic} (current)", tofile=f"{topic} (proposed)"))
    result["diff"] = "".join(diff[:DIFF_LINE_LIMIT])
    if len(diff) > DIFF_LINE_LIMIT:
        result.update({"diff_truncated": True, "diff_total_lines": len(diff)})
    return result


def apply_doc_update(topic: str, description: str = "") -> dict:
    """Apply a pending proposal: archive the old version, write the new one, update the index."""
    with _LOCK:
        try:
            path = _topic_file(topic)
            proposal_file = _proposal_file(topic)
        except DocError as exc:
            return {"status": "error", "message": str(exc)}
        if not proposal_file.exists():
            return {"status": "error", "message": f"No proposal found for '{topic}'. Use propose_doc_update first."}
        proposal = json.loads(_read(proposal_file))
        old = _read(path) if path.exists() else None

        # Refuse to overwrite an edit made after this draft was taken. Proposals
        # written by older server versions carry no hash and are applied as before.
        if "base_sha256" in proposal and proposal["base_sha256"] != _sha(old):
            return {"status": "error", "topic": topic,
                    "message": "The topic changed after this proposal was drafted. Applying it would "
                               "silently revert that change. Re-read the topic and propose again."}

        if old is not None:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            _atomic_write(_inside(ARCHIVE_DIR / f"{topic}-{stamp}.md", ARCHIVE_DIR), old)

        new_content = _FOOTER_RE.sub("\n", proposal["new_content"]).rstrip("\n") + "\n"
        _atomic_write(path, new_content)

        index = _index()
        entry = index.get(topic, {})
        change = description or proposal.get("description", "")
        entry.update({
            "summary": proposal.get("summary") or entry.get("summary") or _derive_summary(new_content),
            "last_change": change,
            "file": str(path.relative_to(DOCS_DIR.resolve())).replace("\\", "/"),
            "updated": datetime.now().isoformat(),
        })
        entry.pop("description", None)
        index[topic] = entry
        _write_index(index)
        proposal_file.unlink()
        _git_commit(f"{topic}: {change or 'update'}")

    return {"status": "applied", "topic": topic, "file": entry["file"],
            "message": "Doc updated; previous version archived."}


def reject_proposal(topic: str) -> dict:
    with _LOCK:
        try:
            proposal_file = _proposal_file(topic)
        except DocError as exc:
            return {"status": "error", "message": str(exc)}
        if not proposal_file.exists():
            return {"status": "error", "message": f"No proposal found for '{topic}'."}
        proposal_file.unlink()
        _git_commit(f"{topic}: proposal rejected")
    return {"status": "rejected", "topic": topic}


def list_proposals() -> dict:
    """Pending drafts, with `stale: true` when the topic changed after the draft was taken."""
    _ensure_dirs()
    out = []
    for pf in sorted(PROPOSALS_DIR.rglob("*.proposal.md")):
        try:
            p = json.loads(_read(pf))
            topic = p.get("topic") or str(pf.relative_to(PROPOSALS_DIR))[:-len(".proposal.md")]
            path = _topic_file(topic)
            current = _read(path) if path.exists() else None
            stale = "base_sha256" in p and p["base_sha256"] != _sha(current)
            out.append({"topic": topic, "description": p.get("description", ""),
                        "proposed_at": p.get("proposed_at", ""), "stale": stale,
                        "new_doc": current is None})
        except Exception as exc:  # noqa: BLE001
            out.append({"file": str(pf.name), "error": str(exc)})
    return {"proposals": out, "total": len(out)}
