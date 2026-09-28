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

v1.4: the proposal system is the gate. Several proposals per topic (one file
each, by id), section-level proposals, status untested -> verified (live
evidence attached) / disputed, and apply only for verified, non-stale,
non-overlapping proposals; readers see open proposals as unverified hints;
applies commit with the evidence. See the "proposals (v1.4)" block below.
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
        legacy = migrate_legacy_proposals()
        if legacy:
            _git_commit(f"Migrate {len(legacy)} v1.3 proposals to v1.4 (status untested): {', '.join(legacy)}")
        result = {"dropped": dropped, "added": added, "migrated": migrated, "legacy_proposals": legacy}
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
        out = {"topic": topic, "section": heading, "line": line, "content": content,
               "status": meta.get("status", "")}
        pending = _pending_hints(topic, span=_span(body.splitlines(), heading), with_text=True)
    elif full:
        out = {"topic": topic, "content": text}
        pending = _pending_hints(topic)
    else:
        plain = [s.strip() for s in body.splitlines() if s.strip() and not s.strip().startswith("#")]
        summary = " ".join(plain)
        if len(summary) > SUMMARY_CHAR_LIMIT:
            summary = summary[:SUMMARY_CHAR_LIMIT] + "…"
        out = {
            "topic": topic,
            "frontmatter": meta,
            "outline": _outline(body),
            "summary": summary,
            "length_chars": len(text),
            "hint": "call get_doc(topic, full=True) for the complete content",
        }
        pending = _pending_hints(topic)
    if pending:
        out["pending_proposals"] = pending
        out["pending_note"] = ("open proposals on this text: `untested` ones are unverified hints; if you verify "
                               "one live, confirm_proposal(id, evidence); if it is wrong, dispute_proposal")
    return out


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
    res = _SEARCH.search(query, limit)
    open_by_topic = {}
    for p in _load_all():
        if p.get("status") != "disputed":
            open_by_topic[p["topic"]] = open_by_topic.get(p["topic"], 0) + 1
    for r in res.get("results", []) if isinstance(res, dict) else []:
        if open_by_topic.get(r.get("topic")):
            r["pending_proposals"] = open_by_topic[r["topic"]]
    return res


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

# --------------------------------------------------------------------------- proposals (v1.4)
#
# One JSON file per proposal: .proposals/<id>.json. Kinds and what "stale" means:
#   doc      whole-topic content (a new topic or a replacement)  -- sha of the topic file
#   section  one section: heading line + body incl. subsections  -- sha of that section's text
#   delete / rename                                              -- sha of the topic file
# Status: `untested` until evidence is attached (on propose, or later with
# confirm_proposal) -> `verified`; `disputed` after dispute_proposal (never applied).
# apply_doc_update applies a proposal only if it is verified, not stale, and does
# not overlap another open (untested/verified) proposal on the same topic --
# overlap = either one covers the whole topic, or the two sections' line ranges
# intersect in the current file. The gate is the proposal system, not a reviewer:
# evidence is what an agent saw live (what ran, what came back, MA3 version).

EVIDENCE_MIN_CHARS = 20
SECTION_TEXT_HINT_LIMIT = 4000
_ID_RE = re.compile(r"^p-\d{8}-\d{6}-[0-9a-f]{4}$")


def _new_id() -> str:
    while True:
        pid = f"p-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"
        if not (PROPOSALS_DIR / f"{pid}.json").exists():
            return pid


def _pfile(pid: str) -> Path:
    if not isinstance(pid, str) or not _ID_RE.match(pid):
        raise DocError(f"Invalid proposal id {pid!r} (expected p-YYYYMMDD-HHMMSS-xxxx).")
    return _inside(PROPOSALS_DIR / f"{pid}.json", PROPOSALS_DIR)


def _save(p: dict):
    _atomic_write(_pfile(p["id"]), json.dumps(p, indent=2, ensure_ascii=False))


def _load_all() -> list[dict]:
    _ensure_dirs()
    out = []
    for pf in sorted(PROPOSALS_DIR.glob("p-*.json")):
        try:
            out.append(json.loads(_read(pf)))
        except Exception as exc:  # noqa: BLE001 -- one bad file must not hide the rest
            log.warning("unreadable proposal %s: %s", pf.name, exc)
    return out


def migrate_legacy_proposals() -> list[str]:
    """v1.3 kept one `<topic>.proposal.md` per topic. Convert each to a v1.4
    proposal (status untested: nobody attached evidence to it)."""
    _ensure_dirs()
    done = []
    for pf in sorted(PROPOSALS_DIR.rglob("*.proposal.md")):
        try:
            old = json.loads(_read(pf))
            topic = old.get("topic") or str(pf.relative_to(PROPOSALS_DIR)).replace("\\", "/")[:-len(".proposal.md")]
            action = old.get("action", "update")
            p = {"id": _new_id(), "topic": topic, "kind": "doc" if action == "update" else action,
                 "description": old.get("description", ""), "summary": old.get("summary", ""),
                 "proposed_at": old.get("proposed_at", ""), "proposed_by": "legacy (v1.3 draft)",
                 "status": "untested", "evidence": []}
            if "base_sha256" in old:
                p["base_sha256"] = old["base_sha256"]
            if action == "update":
                p["new_content"] = old.get("new_content", "")
            if action == "rename":
                p["new_topic"] = old.get("new_topic", "")
            _save(p)
            pf.unlink()
            done.append(f"{topic} -> {p['id']}")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not migrate %s: %s", pf, exc)
    return done


def _body_split(text: str) -> tuple[str, str]:
    """(frontmatter prefix, body) such that prefix + body == text."""
    _, body = _split_frontmatter(text)
    return text[:len(text) - len(body)], body


def _span(lines: list[str], heading_line: str):
    """(start, end) line range of the section headed exactly by heading_line,
    incl. subsections, without trailing blank lines. None if it is not there."""
    heads = _headings("\n".join(lines))
    want = heading_line.strip()
    for n, (i, level, line) in enumerate(heads):
        if line == want:
            end = next((j for j, lv, _ in heads[n + 1:] if lv <= level), len(lines))
            while end > i + 1 and not lines[end - 1].strip():
                end -= 1
            return i, end
    return None


def _section_text(lines: list[str], span) -> str:
    return "\n".join(lines[span[0]:span[1]]) + "\n"


def _state(p: dict) -> dict:
    """stale flag and scope (the section's line range, or 'whole') against the current file."""
    try:
        path = _topic_file(p["topic"])
    except DocError:
        return {"stale": True, "span": None}
    cur = _read(path) if path.exists() else None
    if p.get("kind") == "section":
        if cur is None:
            return {"stale": True, "span": None}
        lines = _body_split(cur)[1].splitlines()
        sp = _span(lines, p.get("section", ""))
        if not sp:
            return {"stale": True, "span": None}
        return {"stale": _sha(_section_text(lines, sp)) != p.get("base_sha256"), "span": sp}
    return {"stale": "base_sha256" in p and p["base_sha256"] != _sha(cur), "span": "whole"}


def _overlap(a, b) -> bool:
    if a == "whole" or b == "whole":
        return True
    if a is None or b is None:
        return False
    return a[0] < b[1] and b[0] < a[1]


def _annotate(props: list[dict]) -> list[dict]:
    """Copies with _stale, _span, _conflicts and _applyable."""
    out = []
    for p in props:
        st = _state(p)
        out.append(dict(p, _stale=st["stale"], _span=st["span"]))
    for a in out:
        a["_conflicts"] = [b["id"] for b in out
                           if b is not a and b["topic"] == a["topic"]
                           and a.get("status") != "disputed" and b.get("status") != "disputed"
                           and _overlap(a["_span"], b["_span"])]
        a["_applyable"] = a.get("status") == "verified" and not a["_stale"] and not a["_conflicts"]
    return out


def _brief(a: dict) -> dict:
    item = {"id": a["id"], "topic": a["topic"], "kind": a.get("kind", "doc"), "status": a.get("status", "untested"),
            "description": a.get("description", ""), "proposed_at": a.get("proposed_at", ""),
            "proposed_by": a.get("proposed_by", ""), "stale": a["_stale"], "conflicts_with": a["_conflicts"],
            "applyable": a["_applyable"], "evidence": len(a.get("evidence", []))}
    if item["kind"] == "section":
        item["section"] = a.get("section", "")
    if item["kind"] == "rename":
        item["new_topic"] = a.get("new_topic", "")
    if item["kind"] == "doc":
        try:
            item["new_doc"] = not _topic_file(a["topic"]).exists()
        except DocError:
            pass
    return item


def _evidence_entry(kind: str, text: str, by: str, ma3_version: str) -> dict:
    text = (text or "").strip()
    if len(text) < EVIDENCE_MIN_CHARS:
        raise DocError(f"evidence must say what ran and what came back (at least {EVIDENCE_MIN_CHARS} chars).")
    e = {"kind": kind, "by": (by or "agent").strip()[:80], "at": datetime.now().isoformat(timespec="seconds"),
         "text": text[:2000]}
    if ma3_version:
        e["ma3_version"] = str(ma3_version).strip()[:40]
    return e


def _new_proposal(topic: str, kind: str, description: str, by: str, evidence: str, ma3_version: str) -> dict:
    p = {"id": _new_id(), "topic": topic, "kind": kind, "description": description,
         "proposed_at": datetime.now().isoformat(timespec="seconds"), "proposed_by": (by or "agent").strip()[:80],
         "status": "untested", "evidence": []}
    if evidence:
        p["evidence"].append(_evidence_entry("propose", evidence, by, ma3_version))
        p["status"] = "verified"
    return p


def _drop_replaced(replaces: str, topic: str):
    if not replaces:
        return
    pf = _pfile(replaces)
    if pf.exists():
        old = json.loads(_read(pf))
        if old.get("topic") != topic:
            raise DocError(f"replaces={replaces} belongs to topic {old.get('topic')!r}, not {topic!r}.")
        pf.unlink()


def _after_propose(p: dict, diff: dict) -> dict:
    mine = next(a for a in _annotate(_load_all()) if a["id"] == p["id"])
    out = _brief(mine)
    out.update(diff)
    if mine["_conflicts"]:
        out["hint"] = ("overlaps other open proposals on this topic -- apply is refused until only one "
                       "remains (reject_proposal the superseded one, or pass replaces=<id> when re-proposing)")
    elif out["status"] == "untested":
        out["hint"] = "untested: confirm_proposal(id, evidence) once it is verified live; then apply_doc_update(id=...)"
    return out


def propose_doc_update(topic: str, new_content: str, description: str = "", summary: str = "",
                       evidence: str = "", by: str = "", ma3_version: str = "", replaces: str = "") -> dict:
    """Draft a whole-topic update (or a new topic). Prefer propose_section_update for edits."""
    with _LOCK:
        try:
            path = _topic_file(topic)
            _drop_replaced(replaces, topic)
            p = _new_proposal(topic, "doc", description, by, evidence, ma3_version)
        except DocError as exc:
            return {"topic": topic, "status": "error", "message": str(exc)}
        old = _read(path) if path.exists() else None
        p.update({"summary": summary, "base_sha256": _sha(old), "new_content": new_content})
        _save(p)
        if old is None:
            preview = new_content[:NEW_DOC_PREVIEW_CHAR_LIMIT] + ("…" if len(new_content) > NEW_DOC_PREVIEW_CHAR_LIMIT else "")
            diff = {"diff": None, "new_doc_preview": preview, "new_doc_lines": len(new_content.splitlines())}
        else:
            diff = _diff(topic, old, new_content, DIFF_LINE_LIMIT)
        return _after_propose(p, diff)


def propose_section_update(topic: str, section: str, new_text: str, description: str = "",
                           evidence: str = "", by: str = "", ma3_version: str = "", replaces: str = "") -> dict:
    """Draft a replacement for ONE section (heading line + body incl. subsections).
    `section` finds the heading like get_doc(section=); `new_text` starts with the
    (possibly edited) heading line. Only this section's text is hashed, so edits
    elsewhere in the topic do not make it stale."""
    with _LOCK:
        try:
            path = _topic_file(topic)
            if not path.exists():
                raise DocError(f"Topic '{topic}' not found (new topics go through propose_doc_update).")
            if not (new_text or "").lstrip().startswith("#"):
                raise DocError("new_text must start with the section's heading line (e.g. '## 3. Title').")
            body = _body_split(_read(path))[1]
            hit = _find_section(body, section)
            if not hit:
                raise DocError(f"No section matching {section!r} in '{topic}'.")
            heading = hit[0]
            lines = body.splitlines()
            sp = _span(lines, heading)
            _drop_replaced(replaces, topic)
            p = _new_proposal(topic, "section", description, by, evidence, ma3_version)
        except DocError as exc:
            return {"topic": topic, "status": "error", "message": str(exc)}
        old_sec = _section_text(lines, sp)
        new_sec = new_text.strip("\n") + "\n"
        p.update({"section": heading, "base_sha256": _sha(old_sec), "new_text": new_sec})
        _save(p)
        return _after_propose(p, _diff(f"{topic} :: {heading}", old_sec, new_sec, DIFF_LINE_LIMIT))


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


def propose_topic_change(topic: str, action: str, new_topic: str = "", description: str = "",
                         evidence: str = "", by: str = "", replaces: str = "") -> dict:
    """Draft a delete or rename. Applied by apply_doc_update once verified, like a content edit."""
    with _LOCK:
        try:
            path = _topic_file(topic)
            if action == "rename":
                _check_topic(new_topic)
            if action not in ("delete", "rename"):
                raise DocError("action must be 'delete' or 'rename'.")
            if not path.exists():
                raise DocError(f"Topic '{topic}' not found.")
            if action == "rename" and (new_topic in _index() or _topic_file(new_topic, {}).exists()):
                raise DocError(f"Topic '{new_topic}' already exists.")
            _drop_replaced(replaces, topic)
            p = _new_proposal(topic, action, description, by, evidence, "")
        except DocError as exc:
            return {"topic": topic, "status": "error", "message": str(exc)}
        p["base_sha256"] = _sha(_read(path))
        if action == "rename":
            p["new_topic"] = new_topic
        _save(p)
        result = _after_propose(p, {"references": _references(topic, exclude=topic)})
    result["hint"] = ("topics in `references` still name the old topic -- propose edits for them "
                      "after this is applied (lint_docs lists broken references)")
    return result


def _select(topic: str = "", pid: str = "") -> tuple[dict | None, dict | None]:
    """(annotated proposal, None) or (None, error dict). By id, or a topic's only open proposal."""
    props = _annotate(_load_all())
    if pid:
        hit = [a for a in props if a["id"] == pid]
        if not hit:
            return None, {"status": "error", "message": f"No proposal {pid!r}."}
        return hit[0], None
    hit = [a for a in props if a["topic"] == topic]
    if not hit:
        return None, {"status": "error", "topic": topic, "message": f"No proposal found for '{topic}'."}
    if len(hit) > 1:
        return None, {"status": "error", "topic": topic,
                      "message": f"'{topic}' has {len(hit)} open proposals -- pass id=.",
                      "proposals": [_brief(a) for a in hit]}
    return hit[0], None


def get_proposal(topic: str = "", id: str = "") -> dict:
    """One proposal in full: status, evidence, stale/conflicts, and the diff (up to
    PROPOSAL_DIFF_LINE_LIMIT lines; for a section proposal, the section's diff)."""
    a, err = _select(topic, id)
    if err:
        return err
    out = _brief(a)
    out.update({"summary": a.get("summary", ""), "evidence_entries": a.get("evidence", [])})
    kind = out["kind"]
    try:
        path = _topic_file(a["topic"])
    except DocError as exc:
        return {"status": "error", "message": str(exc)}
    current = _read(path) if path.exists() else None
    if kind == "doc":
        if current is None:
            out["new_content"] = a.get("new_content", "")[:GET_DOCS_FULL_CHARS]
        else:
            out.update(_diff(a["topic"], current, a.get("new_content", ""), PROPOSAL_DIFF_LINE_LIMIT))
    elif kind == "section":
        if a["_span"] and current is not None:
            lines = _body_split(current)[1].splitlines()
            out.update(_diff(f"{a['topic']} :: {a['section']}", _section_text(lines, a["_span"]),
                             a.get("new_text", ""), PROPOSAL_DIFF_LINE_LIMIT))
        else:
            out["new_text"] = a.get("new_text", "")
    return out


def _mark(topic: str, id: str, kind: str, evidence: str, by: str, ma3_version: str) -> dict:
    with _LOCK:
        a, err = _select(topic, id)
        if err:
            return err
        try:
            entry = _evidence_entry(kind, evidence, by, ma3_version)
        except DocError as exc:
            return {"status": "error", "id": a["id"], "message": str(exc)}
        p = json.loads(_read(_pfile(a["id"])))
        p.setdefault("evidence", []).append(entry)
        note = None
        if kind == "dispute":
            p["status"] = "disputed"
        elif p.get("status") == "disputed":
            note = "stays disputed: counter-evidence exists -- resolve by reject_proposal and a new proposal"
        else:
            p["status"] = "verified"
        _save(p)
        out = _brief(next(x for x in _annotate(_load_all()) if x["id"] == p["id"]))
        if note:
            out["note"] = note
        return out


def confirm_proposal(topic: str = "", id: str = "", evidence: str = "", by: str = "", ma3_version: str = "") -> dict:
    """Attach live evidence that the proposal is right -> status verified."""
    return _mark(topic, id, "confirm", evidence, by, ma3_version)


def dispute_proposal(topic: str = "", id: str = "", evidence: str = "", by: str = "", ma3_version: str = "") -> dict:
    """Attach counter-evidence -> status disputed (never applied)."""
    return _mark(topic, id, "dispute", evidence, by, ma3_version)


def _commit_message(head: str, p: dict) -> str:
    lines = [head, "", f"proposal {p['id']} ({p.get('kind', 'doc')}"
             + (f": {p['section']}" if p.get("kind") == "section" else "") + ")",
             f"proposed-by: {p.get('proposed_by', '?')} {p.get('proposed_at', '')}"]
    for e in p.get("evidence", []):
        ver = f" [MA3 {e['ma3_version']}]" if e.get("ma3_version") else ""
        lines.append(f"{e.get('kind')}: {e.get('by')} {e.get('at')}{ver}: {e.get('text')}")
    return "\n".join(lines)


def apply_doc_update(topic: str = "", description: str = "", id: str = "") -> dict:
    """Apply one proposal -- only if verified, not stale and not overlapping another
    open proposal: archive the old version, write the result, update the index,
    commit with the proposal's evidence in the message."""
    with _LOCK:
        a, err = _select(topic, id)
        if err:
            return err
        pid, topic, kind = a["id"], a["topic"], a.get("kind", "doc")
        if a.get("status") != "verified":
            why = ("disputed: counter-evidence exists" if a.get("status") == "disputed"
                   else "untested: no live evidence attached -- confirm_proposal(id, evidence) first")
            return {"status": "error", "id": pid, "topic": topic, "message": f"Not applied -- {why}."}
        if a["_stale"]:
            return {"status": "error", "id": pid, "topic": topic,
                    "message": "Not applied -- the topic (or this section) changed after the proposal was "
                               "drafted; applying would silently revert that change. Re-read and propose again."}
        if a["_conflicts"]:
            return {"status": "error", "id": pid, "topic": topic, "conflicts_with": a["_conflicts"],
                    "message": "Not applied -- overlaps other open proposals on this topic; "
                               "reject the superseded one(s) first."}
        path = _topic_file(topic)
        pfile = _pfile(pid)
        old = _read(path) if path.exists() else None
        change = description or a.get("description", "")

        if old is not None:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            _atomic_write(_inside(ARCHIVE_DIR / f"{topic}-{stamp}.md", ARCHIVE_DIR), old)

        index = _index()
        if kind == "delete":
            if old is None:
                return {"status": "error", "topic": topic, "message": f"Topic '{topic}' not found."}
            path.unlink()
            index.pop(topic, None)
            _write_index(index)
            pfile.unlink()
            _git_commit(_commit_message(f"{topic}: deleted{' -- ' + change if change else ''}", a))
            return {"status": "applied", "id": pid, "action": "delete", "topic": topic,
                    "message": "Topic deleted; last version archived.", "references": _references(topic)}
        if kind == "rename":
            new_topic = a.get("new_topic", "")
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
            pfile.unlink()
            _git_commit(_commit_message(f"{topic} -> {new_topic}: renamed{' -- ' + change if change else ''}", a))
            return {"status": "applied", "id": pid, "action": "rename", "topic": topic, "new_topic": new_topic,
                    "message": "Topic renamed.", "references": _references(topic)}

        if kind == "section":
            prefix, body = _body_split(old)
            lines = body.splitlines()
            s, e = a["_span"]
            lines[s:e] = a["new_text"].rstrip("\n").splitlines()
            new_content = prefix + "\n".join(lines).rstrip("\n") + "\n"
        else:
            new_content = _FOOTER_RE.sub("\n", a["new_content"]).rstrip("\n") + "\n"
        _atomic_write(path, new_content)

        entry = index.get(topic, {})
        entry.update({
            "summary": a.get("summary") or entry.get("summary") or _derive_summary(new_content),
            "last_change": change,
            "file": str(path.relative_to(DOCS_DIR.resolve())).replace("\\", "/"),
            "updated": datetime.now().isoformat(),
        })
        entry.pop("description", None)
        index[topic] = entry
        _write_index(index)
        pfile.unlink()
        _git_commit(_commit_message(f"{topic}: {change or 'update'}", a))

    return {"status": "applied", "id": pid, "topic": topic, "kind": kind, "file": entry["file"],
            "message": "Doc updated; previous version archived."}


def reject_proposal(topic: str = "", id: str = "") -> dict:
    with _LOCK:
        a, err = _select(topic, id)
        if err:
            return err
        _pfile(a["id"]).unlink()
        _git_commit(f"{a['topic']}: proposal {a['id']} rejected")
    return {"status": "rejected", "id": a["id"], "topic": a["topic"]}


def list_proposals(topic: str = "") -> dict:
    """Open proposals with status, stale, conflicts_with and applyable."""
    items = [_brief(a) for a in _annotate(_load_all()) if not topic or a["topic"] == topic]
    return {"proposals": items, "total": len(items)}


def _pending_hints(topic: str, span=None, with_text: bool = False) -> list[dict]:
    """Open proposals on a topic (optionally only those touching `span`) as
    unverified hints for readers."""
    out = []
    for a in _annotate(_load_all()):
        if a["topic"] != topic or a.get("status") == "disputed":
            continue
        if span is not None and not _overlap(a["_span"], span):
            continue
        h = {"id": a["id"], "status": a.get("status"), "kind": a.get("kind"), "description": a.get("description", "")}
        if a.get("kind") == "section":
            h["section"] = a.get("section", "")
            if with_text:
                h["proposed_text"] = a.get("new_text", "")[:SECTION_TEXT_HINT_LIMIT]
        out.append(h)
    return out


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
