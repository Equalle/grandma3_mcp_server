"""Section-level BM25 search over the docs corpus (v1.3).

Why not substring matching any more: `tag` matched `stage`, a topic that named a
term once ranked with one that is about it, and ties fell back to alphabetical
order. Here:

  * Tokens are whole words. Identifiers also index their parts
    (`GetPresetDataFast` -> getpresetdatafast, get, preset, data, fast), and a
    trailing plural `s` is folded (`recipes` == `recipe`).
  * Each topic is split into sections at #, ## and ### headings (outside code
    fences). BM25 scores sections; heading words count 3x.
  * A topic scores its best section plus a share of the next two, scaled by how
    many query terms it covers, a title/topic-name match, and its status
    (verified > draft > uncertain).
  * Results carry the best sections' headings and an excerpt around the
    matching line, so the caller can fetch just that section.

The index lives in memory and is rebuilt only when a file's mtime/size changes.
"""

import math
import re
import threading
from pathlib import Path

K1, B = 1.2, 0.75
HEADING_WEIGHT = 3
EXCERPT_CHARS = 220

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_PART_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_HEADING_RE = re.compile(r"^(#{1,3}) +(.*\S)\s*$")
_STOP = frozenset("""a an and are as at be by do does for from how i in is it of on or
    the this that to what when where which with without why can use using""".split())


def _fold(word: str) -> str:
    w = word.lower()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _parts(word: str) -> list[str]:
    parts = [p for chunk in word.split("_") for p in _PART_RE.findall(chunk)]
    return parts if len(parts) > 1 else []


def tokenize(text: str) -> list[str]:
    out = []
    for m in _WORD_RE.finditer(text):
        w = m.group()
        out.append(_fold(w))
        for p in _parts(w):
            if len(p) > 1:
                out.append(_fold(p))
    return out


def query_terms(query: str, vocab: set) -> list[str]:
    """Whole words of the query; an identifier the corpus does not contain as a
    whole falls back to its parts. Stop words drop unless nothing else is left."""
    terms = []
    for m in _WORD_RE.finditer(query or ""):
        w = m.group()
        f = _fold(w)
        if f in vocab or not _parts(w):
            terms.append(f)
        else:
            terms.extend(_fold(p) for p in _parts(w) if len(p) > 1)
    kept = [t for t in terms if t not in _STOP and len(t) > 1]
    return list(dict.fromkeys(kept or terms))


# --------------------------------------------------------------------------- index

class _Section:
    __slots__ = ("heading", "level", "line", "lines", "tf", "length")

    def __init__(self, heading, level, line):
        self.heading, self.level, self.line, self.lines = heading, level, line, []


class _Doc:
    __slots__ = ("topic", "title", "status", "name_terms", "title_terms", "sections")


def _status_factor(status: str) -> float:
    s = (status or "").lower()
    if s.startswith(("live-verified", "verified", "structure live-read", "facts live-verified")):
        return 1.1
    if "uncertain" in s or s.startswith("partial"):
        return 0.95
    return 1.0


def split_sections(body: str) -> list[_Section]:
    """Sections at #/##/### headings outside code fences. Text before the first
    heading is a section with an empty heading. Line numbers are 1-based in body."""
    sections = [_Section("", 0, 1)]
    fenced = False
    for i, line in enumerate(body.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        m = None if fenced else _HEADING_RE.match(line)
        if m:
            sections.append(_Section(m.group(2).strip(), len(m.group(1)), i))
        else:
            sections[-1].lines.append(line)
    return [s for s in sections if s.heading or any(l.strip() for l in s.lines)]


class SearchIndex:
    def __init__(self, split_frontmatter):
        self._split = split_frontmatter
        self._lock = threading.Lock()
        self._sig = None
        self.docs: list[_Doc] = []
        self.df: dict[str, int] = {}
        self.n_sections = 0
        self.avg_len = 1.0

    def ensure(self, files: list[tuple[str, Path]]):
        sig = tuple((t, p.stat().st_mtime_ns, p.stat().st_size) for t, p in files)
        if sig == self._sig:
            return
        with self._lock:
            if sig == self._sig:
                return
            self._build(files)
            self._sig = sig

    def _build(self, files):
        docs, df, total, count = [], {}, 0, 0
        for topic, path in files:
            meta, body = self._split(path.read_text(encoding="utf-8"))
            d = _Doc()
            d.topic, d.status = topic, meta.get("status", "")
            h1 = next((l[2:].strip() for l in body.splitlines() if l.startswith("# ")), "")
            d.title = meta.get("title") or h1
            d.name_terms = set(tokenize(topic.replace("-", " ").replace("/", " ")))
            d.title_terms = set(tokenize(d.title))
            d.sections = split_sections(body)
            for s in d.sections:
                tf = {}
                for t in tokenize("\n".join(s.lines)):
                    tf[t] = tf.get(t, 0) + 1
                for t in tokenize(s.heading):
                    tf[t] = tf.get(t, 0) + HEADING_WEIGHT
                s.tf, s.length = tf, sum(tf.values()) or 1
                for t in tf:
                    df[t] = df.get(t, 0) + 1
                total += s.length
                count += 1
            docs.append(d)
        self.docs, self.df = docs, df
        self.n_sections, self.avg_len = count, (total / count if count else 1.0)

    # ----------------------------------------------------------------------- query

    def search(self, query: str, limit: int) -> dict:
        terms = query_terms(query, set(self.df))
        if not terms:
            return {"query_terms": [], "results": [], "total_matches": 0}
        idf = {t: math.log(1 + (self.n_sections - self.df.get(t, 0) + 0.5) / (self.df.get(t, 0) + 0.5))
               for t in terms}
        scored = []
        for d in self.docs:
            sec_scores, matched = [], set()
            for s in d.sections:
                sc = 0.0
                for t in terms:
                    f = s.tf.get(t)
                    if f:
                        matched.add(t)
                        sc += idf[t] * f * (K1 + 1) / (f + K1 * (1 - B + B * s.length / self.avg_len))
                if sc:
                    sec_scores.append((sc, s))
            if not sec_scores:
                continue
            sec_scores.sort(key=lambda x: x[0], reverse=True)
            score = sec_scores[0][0] + 0.25 * sum(x[0] for x in sec_scores[1:3])
            coverage = len(matched) / len(terms)
            named = len(set(terms) & (d.name_terms | d.title_terms)) / len(terms)
            score *= (0.5 + 0.5 * coverage) * (1 + 0.6 * named) * _status_factor(d.status)
            scored.append((score, d, matched, sec_scores[:2]))
        scored.sort(key=lambda x: (-x[0], x[1].topic))
        results = []
        for score, d, matched, best in scored[:limit]:
            results.append({
                "topic": d.topic,
                "title": d.title,
                "status": (d.status or "")[:40],
                "score": round(score, 2),
                "matched_terms": [t for t in terms if t in matched],
                "sections": [{"heading": s.heading or "(intro)", "excerpt": _excerpt(s, terms)}
                             for _, s in best],
            })
        return {"query_terms": terms, "results": results, "total_matches": len(scored)}


def _excerpt(section: _Section, terms: list[str]) -> str:
    """The line with most query-term hits (plus the next line), cut around the first hit."""
    tset, best, best_i = set(terms), -1, 0
    for i, line in enumerate(section.lines):
        hits = sum(1 for t in tokenize(line) if t in tset)
        if hits > best:
            best, best_i = hits, i
    text = " ".join(l.strip() for l in section.lines[best_i:best_i + 2] if l.strip())
    if len(text) <= EXCERPT_CHARS:
        return text
    low = text.lower()
    first = min((p for p in (low.find(t) for t in terms) if p >= 0), default=0)
    start = max(0, first - EXCERPT_CHARS // 3)
    return ("…" if start else "") + text[start:start + EXCERPT_CHARS].strip() + "…"
