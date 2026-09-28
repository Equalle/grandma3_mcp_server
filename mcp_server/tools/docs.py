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

v1.3: section-level BM25 search (search.py), section fetch, multi-topic fetch,
get_proposal (full diff), lint_docs, and rename/delete proposals that go through
the same apply_doc_update approval step as content edits.
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

from mcp_server.tools import search as _search

log = logging.getLogger(__name__)

DOCS_DIR = Path(os.environ.get("MCP_DOCS_DIR", "/config/mcp_docs"))
PROPOSALS_DIR = DOCS_DIR / ".proposals"
ARCHIVE_DIR = DOCS_DIR / ".archive"
HIDDEN_PARTS = {".archive", ".proposals", ".git"}

LIST_LIMIT = 50
SUMMARY_CHAR_LIMIT = 300
TOPIC_SUMMARY_LIMIT = 200
SEARCH_RESULT_LIMIT = 5
SEARCH_RESULT_MAX = 15
GET_DOCS_MAX = 8
GET_DOCS_FULL_CHARS = 60000
DIFF_LINE_LIMIT = 100
PROPOSAL_DIFF_LINE_LIMIT = 400
LARGE_DOC_CHARS = 40000
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
    """What the topic covers: frontmatter summary/description, else its
    `**Scope:**` line, else the title / H1."""
    meta, body = _split_frontmatter(text)
    for key in ("summary", "description"):
        if meta.get(key):
            return meta[key][:TOPIC_SUMMARY_LIMIT]
    m = re.search(r"^\*\*Scope:\*\*\s*(.+(?:\n(?!\*\*|\s*$|#).+)*)", body, re.M)
    if m:
        return " ".join(m.group(1).split())[:TOPIC_SUMMARY_LIMIT]
    if meta.get("title"):
        return meta["title"][:TOPIC_SUMMARY_LIMIT]
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:TOPIC_SUMMARY_LIMIT]
    return ""


def _outline(body: str) -> list[str]:
    return [line for _, _, line in _headings(body)]


def _headings(body: str) -> list[tuple[int, int, str]]:
    """(line index, level, heading line) for every heading outside code fences."""
    out, fenced = [], False
    for i, line in enumerate(body.splitlines()):
        s = line.strip()
        if s.startswith("```"):
            fenced = not fenced
            continue
        m = None if fenced else re.match(r"^(#{1,6}) +\S", s)
        if m:
            out.append((i, len(m.group(1)), s))
    return out


def _heading_key(heading_line: str) -> str:
    return re.sub(r"^#+\s*", "", heading_line).strip().lower()


def _find_section(body: str, section: str):
    """The section whose heading best matches `section` (exact, then prefix, then
    substring; numbering like '§5' or '5.' may be omitted). Returns (heading,
    line number, text incl. subsections) or None."""
    q = re.sub(r"^#+\s*", "", section or "").strip().lower()
    if not q:
        return None
    heads = _headings(body)
    bare = lambda h: re.sub(r"^(§\s*)?[\d.]+[.)]?\s+", "", h)
    for test in (lambda h: h == q or bare(h) == q,
                 lambda h: h.startswith(q) or bare(h).startswith(q),
                 lambda h: q in h):
        for n, (i, level, line) in enumerate(heads):
            if test(_heading_key(line)):
                end = next((j for j, lv, _ in heads[n + 1:] if lv <= level), None)
                lines = body.splitlines()
                return line, i + 1, "\n".join(lines[i:end]).rstrip() + "\n"
    return None


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


def get_doc(topic: str, full: bool = False, section: str = "") -> dict:
    try:
        path = _topic_file(topic)
    except DocError as exc:
        return {"topic": topic, "error": str(exc)}
    if not path.exists():
        return {"topic": topic, "error": f"Topic '{topic}' not found."}
    text = _read(path)
    meta, body = _split_frontmatter(text)
    if section:
        hit = _find_section(body, section)
        if not hit:
            return {"topic": topic, "error": f"No section matching {section!r}.", "outline": _outline(body)}
        heading, line, content = hit
        return {"topic": topic, "section": heading, "line": line, "content": content,
                "status": meta.get("status", "")}
    if full:
        return {"topic": topic, "content": text}
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


def get_docs(topics: list, full: bool = False, section: str = "") -> dict:
    """Several topics in one call (outline mode by default). Full content is capped
    at GET_DOCS_FULL_CHARS in total; topics past the cap are listed as omitted."""
    topics = [t for t in (topics or []) if isinstance(t, str)]
    docs, omitted, used = [], [], 0
    for t in topics[:GET_DOCS_MAX]:
        d = get_doc(t, full=full, section=section)
        size = len(d.get("content", ""))
        if size and used + size > GET_DOCS_FULL_CHARS and docs:
            omitted.append(t)
            continue
        used += size
        docs.append(d)
    out = {"docs": docs}
    if omitted:
        out["omitted"] = omitted
        out["hint"] = "response size cap reached: fetch the omitted topics separately or by section"
    if len(topics) > GET_DOCS_MAX:
        out["not_fetched"] = topics[GET_DOCS_MAX:]
    return out


def _topic_files() -> list[tuple[str, Path]]:
    return [(str(md.relative_to(DOCS_DIR).with_suffix("")).replace("\\", "/"), md) for md in _visible_md()]


_SEARCH = _search.SearchIndex(_split_frontmatter)


def search_docs(query: str, limit: int = SEARCH_RESULT_LIMIT) -> dict:
    """Section-level BM25 search (see search.py). Each result names its best
    sections; get_doc(topic, section=<heading>) fetches one."""
    limit = max(1, min(int(limit or SEARCH_RESULT_LIMIT), SEARCH_RESULT_MAX))
    _SEARCH.ensure(_topic_files())
    return _SEARCH.search(query, limit)


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
    result.update(_diff(topic, old, new_content, DIFF_LINE_LIMIT))
    return result


def _diff(topic: str, old: str, new: str, limit: int) -> dict:
    if old == new:
        return {"diff": "(no changes)"}
    import difflib
    diff = list(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                     fromfile=f"{topic} (current)", tofile=f"{topic} (proposed)"))
    out = {"diff": "".join(diff[:limit])}
    if len(diff) > limit:
        out.update({"diff_truncated": True, "diff_total_lines": len(diff)})
    return out


def _references(name: str, exclude: str = "") -> list[str]:
    """Topics whose text names `name` as a whole token (e.g. `name` or name.md)."""
    pat = re.compile(r"(?<![\w/-])" + re.escape(name) + r"(?![\w-])")
    return [t for t, md in _topic_files() if t != exclude and pat.search(_read(md))]


def propose_topic_change(topic: str, action: str, new_topic: str = "", description: str = "") -> dict:
    """Draft a delete or rename. Applied (after approval) by apply_doc_update, like a content edit."""
    with _LOCK:
        try:
            path = _topic_file(topic)
            proposal_file = _proposal_file(topic)
            if action == "rename":
                _check_topic(new_topic)
        except DocError as exc:
            return {"topic": topic, "status": "error", "message": str(exc)}
        if action not in ("delete", "rename"):
            return {"topic": topic, "status": "error", "message": "action must be 'delete' or 'rename'."}
        if not path.exists():
            return {"topic": topic, "status": "error", "message": f"Topic '{topic}' not found."}
        if action == "rename" and (new_topic in _index() or _topic_file(new_topic, {}).exists()):
            return {"topic": topic, "status": "error", "message": f"Topic '{new_topic}' already exists."}
        _ensure_dirs()
        replaced_pending = proposal_file.exists()
        _atomic_write(proposal_file, json.dumps({
            "topic": topic, "action": action, "new_topic": new_topic if action == "rename" else "",
            "description": description, "proposed_at": datetime.now().isoformat(),
            "base_sha256": _sha(_read(path)),
        }, indent=2, ensure_ascii=False))
    result = {"topic": topic, "status": "pending_review", "action": action,
              "references": _references(topic, exclude=topic)}
    if action == "rename":
        result["new_topic"] = new_topic
    if replaced_pending:
        result["replaced_pending_proposal"] = True
    result["hint"] = ("topics in `references` still name the old topic -- propose edits for them "
                      "after this is applied (lint_docs lists broken references)")
    return result


def get_proposal(topic: str) -> dict:
    """One pending proposal with its full diff (up to PROPOSAL_DIFF_LINE_LIMIT lines)."""
    try:
        path = _topic_file(topic)
        proposal_file = _proposal_file(topic)
    except DocError as exc:
        return {"topic": topic, "error": str(exc)}
    if not proposal_file.exists():
        return {"topic": topic, "error": f"No proposal found for '{topic}'."}
    p = json.loads(_read(proposal_file))
    current = _read(path) if path.exists() else None
    out = {"topic": topic, "action": p.get("action", "update"), "description": p.get("description", ""),
           "summary": p.get("summary", ""), "proposed_at": p.get("proposed_at", ""),
           "stale": "base_sha256" in p and p["base_sha256"] != _sha(current), "new_doc": current is None}
    if out["action"] == "rename":
        out["new_topic"] = p.get("new_topic", "")
    elif out["action"] == "update":
        if current is None:
            out["new_content"] = p["new_content"][:GET_DOCS_FULL_CHARS]
        else:
            out.update(_diff(topic, current, p["new_content"], PROPOSAL_DIFF_LINE_LIMIT))
    return out


def apply_doc_update(topic: str, description: str = "") -> dict:
    """Apply a pending proposal (edit, new topic, delete or rename): archive the old
    version, write the result, update the index, commit."""
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

        action = proposal.get("action", "update")
        change = description or proposal.get("description", "")
        index = _index()
        if action == "delete":
            if old is None:
                return {"status": "error", "topic": topic, "message": f"Topic '{topic}' not found."}
            path.unlink()
            index.pop(topic, None)
            _write_index(index)
            proposal_file.unlink()
            _git_commit(f"{topic}: deleted{' -- ' + change if change else ''}")
            return {"status": "applied", "action": "delete", "topic": topic,
                    "message": "Topic deleted; last version archived.",
                    "references": _references(topic)}
        if action == "rename":
            new_topic = proposal.get("new_topic", "")
            try:
                new_path = _topic_file(new_topic, {})
            except DocError as exc:
                return {"status": "error", "topic": topic, "message": str(exc)}
            if old is None or new_topic in index or new_path.exists():
                return {"status": "error", "topic": topic,
                        "message": f"Cannot rename: '{topic}' missing or '{new_topic}' already exists."}
            new_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, new_path)
            entry = index.pop(topic, {})
            entry.update({"file": str(new_path.relative_to(DOCS_DIR.resolve())).replace("\\", "/"),
                          "last_change": change or f"renamed from {topic}",
                          "updated": datetime.now().isoformat()})
            index[new_topic] = entry
            _write_index(index)
            proposal_file.unlink()
            _git_commit(f"{topic} -> {new_topic}: renamed{' -- ' + change if change else ''}")
            return {"status": "applied", "action": "rename", "topic": topic, "new_topic": new_topic,
                    "message": "Topic renamed.", "references": _references(topic)}

        new_content = _FOOTER_RE.sub("\n", proposal["new_content"]).rstrip("\n") + "\n"
        _atomic_write(path, new_content)

        entry = index.get(topic, {})
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
            item = {"topic": topic, "action": p.get("action", "update"), "description": p.get("description", ""),
                    "proposed_at": p.get("proposed_at", ""), "stale": stale, "new_doc": current is None}
            if p.get("new_topic"):
                item["new_topic"] = p["new_topic"]
            out.append(item)
        except Exception as exc:  # noqa: BLE001
            out.append({"file": str(pf.name), "error": str(exc)})
    return {"proposals": out, "total": len(out)}


# --------------------------------------------------------------------------- lint

_LINK_RE = re.compile(r"\]\((?!https?:|#|mailto:)([^)\s#]+\.md)(?:#[^)]*)?\)")
_TICK_REF_RE = re.compile(r"`([a-z0-9]+(?:-[a-z0-9.]+)+)`")


def lint_docs(topic: str = "") -> dict:
    """Read-only corpus check. error: broken structure or links; warn: missing
    metadata, references to topics that do not exist, stale proposals; info:
    placeholder summaries, very large topics."""
    index = _index()
    files = _topic_files()
    names = {t for t, _ in files}
    families = {t.split("-")[0] for t in names}
    issues = []

    def add(level, t, msg):
        issues.append({"level": level, "topic": t, "issue": msg})

    for t, md in files:
        if topic and t != topic:
            continue
        text = _read(md)
        meta, body = _split_frontmatter(text)
        if not text.startswith("---\n"):
            add("warn", t, "no frontmatter (title/status)")
        else:
            for key in ("title", "status"):
                if not meta.get(key):
                    add("warn", t, f"frontmatter has no `{key}`")
        if sum(1 for line in body.splitlines() if line.lstrip().startswith("```")) % 2:
            add("error", t, "unclosed code fence")
        if not any(line.startswith("# ") for line in body.splitlines()):
            add("warn", t, "no H1 heading")
        if _FOOTER_RE.search(text):
            add("warn", t, "legacy 'Last updated' footer")
        for m in _LINK_RE.finditer(body):
            target = (md.parent / m.group(1)).resolve()
            if not target.exists():
                add("error", t, f"broken link {m.group(1)}")
        missing = sorted({m.group(1) for m in _TICK_REF_RE.finditer(body)
                          if m.group(1).split("-")[0] in families and m.group(1) not in names
                          and not re.search(r"\.[a-z]+$", m.group(1))})   # file names, not topics
        if missing:
            add("warn", t, "names topics that do not exist: " + ", ".join(missing[:8]))
        entry = index.get(t)
        if entry is None:
            add("error", t, "not in index.json")
        elif (entry.get("summary") or "") in ("", meta.get("title", "")):
            add("info", t, "index summary is empty or just the title")
        if len(text) > LARGE_DOC_CHARS:
            add("info", t, f"{len(text)} chars -- consider splitting or section fetches")
    if not topic:
        for t in index:
            if t not in names:
                add("error", t, "index entry without a file")
        for p in list_proposals()["proposals"]:
            if p.get("stale"):
                add("warn", p.get("topic", "?"), "pending proposal is stale (topic changed since the draft)")
    order = {"error": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda i: (order[i["level"]], i["topic"]))
    counts = {lv: sum(1 for i in issues if i["level"] == lv) for lv in order}
    return {"topics_checked": 1 if topic else len(files), "counts": counts,
            "issues": issues[:LIST_LIMIT * 2], "truncated": len(issues) > LIST_LIMIT * 2}
