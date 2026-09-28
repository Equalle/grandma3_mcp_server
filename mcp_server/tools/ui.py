"""grandMA3 UI reference: the screenshot <-> UIXML catalog, the lib_menus corpus, colour tokens, icons.

Data (per MA3 version) under UI_ROOT (default /config/ma3_ui), written by MA3-Terminal
`tools/ui-catalog.mjs --export` (the reviewed reference set, not every menu):
    <ver>/catalog/index.json, reference.json, xml-index.json, menus/*.json, shots/*.png
    <ver>/lib_menus/**.uixml|xml|lua                                     -- shared/resource/lib_menus
    <ver>/lib_color_themes/default.xml
    <ver>/textures/graphics.textures.xml + *.tga
Widget rects in the catalog are image pixels of their state's screenshot.
"""
from __future__ import annotations

import io
import json
import os
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

UI_ROOT = Path(os.environ.get("MA3_UI_ROOT", "/config/ma3_ui"))
MAX_SIDE = 1600          # images are scaled to fit this (long side) unless scale is given


# ---------------------------------------------------------------- versions
def versions() -> list[str]:
    if not UI_ROOT.is_dir():
        return []
    return sorted((d.name for d in UI_ROOT.iterdir() if d.is_dir() and (d / "catalog" / "index.json").is_file()),
                  key=lambda v: [int(x) if x.isdigit() else x for x in re.split(r"[.]", v)])


def _ver(version: str) -> str:
    vs = versions()
    if not vs:
        raise FileNotFoundError(f"no UI catalog under {UI_ROOT} (expected <version>/catalog/index.json)")
    if not version:
        return vs[-1]
    hits = [v for v in vs if v == version or v.startswith(version)]
    if not hits:
        raise FileNotFoundError(f"no UI catalog for version {version!r}; have {', '.join(vs)}")
    return hits[-1]


# ---------------------------------------------------------------- catalog
@lru_cache(maxsize=4)
def _catalog(ver: str) -> dict:
    base = UI_ROOT / ver / "catalog"
    index = json.loads((base / "index.json").read_text(encoding="utf-8"))
    menus, widgets = {}, []
    for name, entry in index.get("menus", {}).items():
        if entry.get("status") != "ok":
            continue
        f = base / "menus" / (re.sub(r"[^\w.-]+", "_", name)[:60] + ".json")
        if not f.is_file():
            continue
        m = json.loads(f.read_text(encoding="utf-8"))
        menus[name] = m
        for si, s in enumerate(m.get("states", [])):
            for w in s.get("widgets", []):
                widgets.append((name, si, w))
    xml_index = {}
    xf = base / "xml-index.json"
    if xf.is_file():
        xml_index = json.loads(xf.read_text(encoding="utf-8"))
    return {"index": index, "menus": menus, "widgets": widgets, "xml_index": xml_index, "base": base}


def _xref(w: dict) -> str:
    x = w.get("xml")
    if x:
        return f"{x['file']}:{x['line']}" + (" (via another file)" if x.get("global") else "")
    f = w.get("from")
    return f"runtime-built inside {f['tag']} at {f['file']}:{f['line']}" if f else "not in any .uixml"


def _terms(q: str) -> list[str]:
    return [t for t in re.split(r"\s+", q.lower().strip()) if t]


def catalog_status(version: str = "") -> dict:
    vs = versions()
    if not vs:
        return {"versions": [], "note": f"no catalog under {UI_ROOT}"}
    cat = _catalog(_ver(version))
    kinds: dict[str, int] = {}
    for m in cat["menus"].values():
        kinds[m.get("kind", "menu")] = kinds.get(m.get("kind", "menu"), 0) + 1
    states = sum(len(m.get("states", [])) for m in cat["menus"].values())
    return {"versions": vs, "version": _ver(version), "menus": len(cat["menus"]), "by_kind": kinds,
            "states": states, "widgets": len(cat["widgets"]), "uixml_files_seen": len(cat["xml_index"])}


def ui_examples(pattern: str = "", version: str = "") -> dict:
    """The reviewed reference set: native MA3 menus whose screenshot the .uixml explains, one per layout."""
    ver = _ver(version)
    cat = _catalog(ver)
    ref = {}
    rf = cat["base"] / "reference.json"
    if rf.is_file():
        ref = json.loads(rf.read_text(encoding="utf-8"))
    pats = ref.get("patterns") or {}
    out = []
    for name, m in cat["menus"].items():
        if pattern and pattern.lower() not in [p.lower() for p in m.get("patterns", [])]:
            continue
        xml = m.get("root", {}).get("xml") or {}
        out.append({"menu": name, "kind": m.get("kind", "menu"), "shows": m.get("why", ""),
                    "patterns": [p for p in m.get("patterns", []) if p != "titlebar"],
                    "uixml": xml.get("file") or m.get("xml"), "states": [s["state"] for s in m.get("states", [])],
                    **({"same_layout_as": m["similar"][:6] + ([f"+{len(m['similar']) - 6} more"] if len(m["similar"]) > 6 else [])}
                       if m.get("similar") else {}),
                    **({"note": m["note"]} if m.get("note") else {})})
    res = {"version": ver, "patterns": {p: len(v) for p, v in sorted(pats.items())}, "menus": out,
           "next": "ui_view(menu) shows it with a legend (each widget's .uixml file:line); ui_xml(file) opens the source"}
    if pattern and not out:
        res["error"] = f"no reference menu shows pattern {pattern!r}"
    if not pattern:
        res["selection"] = ref.get("rule", "")
    return res


def ui_find(query: str, limit: int = 10, kind: str = "", version: str = "") -> dict:
    ver = _ver(version)
    cat = _catalog(ver)
    terms = _terms(query)
    if not terms:
        return {"error": "query is empty"}
    hits = []
    for name, si, w in cat["widgets"]:
        m = cat["menus"][name]
        if kind and m.get("kind", "menu") != kind:
            continue
        text, wname, cls = (w.get("text") or "").lower(), (w.get("name") or "").lower(), w["class"].lower()
        hay = f"{text}\0{wname}\0{cls}\0{name.lower()}\0{m['states'][si]['state'].lower()}"
        if not all(t in hay for t in terms):
            continue
        score = 0
        for t in terms:
            score += 6 if text == t else 3 if t in text else 2 if t in wname else 1
        if w.get("hidden"):
            score -= 3
        if si == 0:
            score += 1
        if w.get("xml"):
            score += 1
        hits.append((score, name, si, w))
    hits.sort(key=lambda h: -h[0])
    seen, out = set(), []
    for score, name, si, w in hits:
        key = (name, w["class"], w.get("name"), w.get("text"), _xref(w))
        if key in seen:
            continue
        seen.add(key)
        s = cat["menus"][name]["states"][si]
        out.append({"menu": name, "kind": cat["menus"][name].get("kind", "menu"), "state": s["state"],
                    "widget": {"id": w["id"], "class": w["class"], "name": w.get("name"), "text": w.get("text"),
                               "rect": w["rect"], **({"hidden": True} if w.get("hidden") else {})},
                    "xml": _xref(w), "view": f"ui_view(menu={name!r}, state={s['state']!r}, widget={w['id']})"})
        if len(out) >= limit:
            break
    return {"version": ver, "query": query, "hits": out, "total": len(hits)}


def _load_png(path: Path):
    from PIL import Image
    return Image.open(path).convert("RGBA")


def _png_bytes(img, scale: float) -> bytes:
    from PIL import Image
    if not scale:
        long_side = max(img.size)
        scale = min(1.0, MAX_SIDE / long_side) if long_side else 1.0
    if scale != 1.0:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _find_widget(state: dict, widget: str) -> dict | None:
    if widget == "":
        return None
    ws = state["widgets"]
    if str(widget).isdigit():
        return next((w for w in ws if w["id"] == int(widget)), None)
    wl = str(widget).lower()
    return (next((w for w in ws if (w.get("name") or "").lower() == wl), None)
            or next((w for w in ws if (w.get("text") or "").lower() == wl), None)
            or next((w for w in ws if wl in (w.get("text") or "").lower() or wl in (w.get("name") or "").lower()), None))


def ui_view(menu: str, state: str = "", widget: str = "", pad: int = 24, scale: float = 0.0,
            version: str = "") -> tuple[dict, bytes | None]:
    ver = _ver(version)
    cat = _catalog(ver)
    m = cat["menus"].get(menu) or next((v for k, v in cat["menus"].items() if k.lower() == menu.lower()), None)
    if not m:
        close = [k for k in cat["menus"] if menu.lower() in k.lower()][:15]
        return {"error": f"no menu {menu!r} in the {ver} catalog", "did_you_mean": close}, None
    st = next((s for s in m["states"] if s["state"] == state), None) if state else m["states"][0]
    if not st:
        return {"error": f"no state {state!r}", "states": [s["state"] for s in m["states"]]}, None
    if not st.get("shot"):
        return {"error": "this state has no screenshot"}, None
    img = _load_png(cat["base"] / st["shot"])
    box = (0, 0, img.width, img.height)
    focus = _find_widget(st, widget)
    if widget != "" and not focus:
        return {"error": f"no widget {widget!r} in {menu} / {st['state']} (id, Name or Text)"}, None
    if focus:
        x, y, w, h = focus["rect"]
        box = (max(0, x - pad), max(0, y - pad), min(img.width, x + w + pad), min(img.height, y + h + pad))
        img = img.crop(box)
    inside = [w for w in st["widgets"]
              if not w.get("hidden") and w["rect"][2] > 0 and w["rect"][0] >= box[0] and w["rect"][1] >= box[1]
              and w["rect"][0] + w["rect"][2] <= box[2] and w["rect"][1] + w["rect"][3] <= box[3]]
    legend = [{"id": w["id"], "class": w["class"], "name": w.get("name"), "text": w.get("text"),
               "rect": [w["rect"][0] - box[0], w["rect"][1] - box[1], w["rect"][2], w["rect"][3]], "xml": _xref(w)}
              for w in inside if w.get("text") or w.get("xml")][:60]
    data = _png_bytes(img, scale)
    info = {"version": ver, "menu": menu, "kind": m.get("kind", "menu"), "shows": m.get("why"),
            "patterns": m.get("patterns"), "state": st["state"],
            "states": [s["state"] for s in m["states"]], "xml": m.get("xml"), "root_xml": _xref({"xml": m["root"].get("xml")}) if m.get("root", {}).get("xml") else None,
            "crop": list(box), "legend (rects in this image, before scaling)": legend}
    if focus:
        info["focus"] = {"id": focus["id"], "class": focus["class"], "name": focus.get("name"), "text": focus.get("text"),
                         "xml": _xref(focus)}
    return info, data


def ui_xml(file: str, line: int = 0, context: int = 10, show: bool = False,
           version: str = "") -> tuple[dict, bytes | None]:
    ver = _ver(version)
    cat = _catalog(ver)
    rel = file.replace("\\", "/").lstrip("/")
    rel = re.sub(r"^.*?lib_menus/", "", rel)
    src_path = UI_ROOT / ver / "lib_menus" / rel
    if not src_path.is_file():
        cands = [k for k in cat["xml_index"] if k.endswith(rel)] or \
                [str(p.relative_to(UI_ROOT / ver / "lib_menus")).replace("\\", "/")
                 for p in (UI_ROOT / ver / "lib_menus").rglob(Path(rel).name)]
        if len(cands) == 1:
            rel = cands[0]
            src_path = UI_ROOT / ver / "lib_menus" / rel
        else:
            return {"error": f"no {file!r} in lib_menus {ver}", "candidates": cands[:15]}, None
    lines = src_path.read_text(encoding="utf-8", errors="replace").lstrip("﻿").splitlines()
    seen = cat["xml_index"].get(rel, {})
    info: dict = {"version": ver, "file": rel, "lines": len(lines), "menus": seen.get("menus", [])}
    if line:
        lo, hi = max(1, line - context), min(len(lines), line + context)
        info["snippet"] = "\n".join(f"{i:5}{'>' if i == line else ' '} {lines[i - 1]}" for i in range(lo, hi + 1))
        hits = seen.get("lines", {}).get(str(line), [])
        info["seen_at"] = [{k: h[k] for k in ("menu", "state", "shot", "rect", "text") if k in h} for h in hits[:20]]
        if not hits:
            info["note"] = "this element is not on any captured screenshot (not crawled, hidden, or built only in some state)"
    else:
        info["elements_seen"] = {ln: len(h) for ln, h in sorted(seen.get("lines", {}).items(), key=lambda kv: int(kv[0]))}
        info["head"] = "\n".join(f"{i + 1:5}  {l}" for i, l in enumerate(lines[:40]))
    data = None
    if show and line and info.get("seen_at"):
        h = info["seen_at"][0]
        x, y, w, hh = h["rect"]
        img = _load_png(cat["base"] / h["shot"])
        box = (max(0, x - 24), max(0, y - 24), min(img.width, x + w + 24), min(img.height, y + hh + 24))
        data = _png_bytes(img.crop(box), 0.0)
        info["image"] = f"{h['menu']} / {h['state']}, crop {list(box)}"
    return info, data


# ---------------------------------------------------------------- widget classes (building a UI)
# <ver>/classes.json -- MA3-Terminal tools/ui-classes.mjs --build: per UIXML class its base classes, live property
# list (type, enum values, default, own vs inherited), events, how MA3's own .uixml uses it, nesting rules and the
# hand-tested facts (MA3-Terminal tools/ui-tested.json: per-property effect/notes, per-class notes + probe images under
# <ver>/images/<Class>/).
@lru_cache(maxsize=4)
def _classes(ver: str) -> dict:
    f = UI_ROOT / ver / "classes.json"
    if not f.is_file():
        raise FileNotFoundError(f"no classes.json for {ver} under {UI_ROOT}")
    return json.loads(f.read_text(encoding="utf-8"))["classes"]


def _class_versions() -> list[str]:
    if not UI_ROOT.is_dir():
        return []
    return sorted((d.name for d in UI_ROOT.iterdir() if (d / "classes.json").is_file()),
                  key=lambda v: [int(x) if x.isdigit() else x for x in re.split(r"[.]", v)])


def _cver(version: str) -> str:
    vs = _class_versions()
    if not vs:
        raise FileNotFoundError(f"no classes.json under {UI_ROOT}")
    hits = [v for v in vs if not version or v == version or v.startswith(version)]
    if not hits:
        raise FileNotFoundError(f"no class data for version {version!r}; have {', '.join(vs)}")
    return hits[-1]


def _vals(p: dict, n: int = 12) -> str:
    if p.get("enum") == "YesNo":
        return "Yes/No"
    v = p.get("values") or []
    names = [x.split("=")[0] for x in v if x.split("=")[0].isprintable()]
    return ", ".join(names[:n]) + (f", … ({len(names)})" if len(names) > n else "")


def _confirmed(p: dict) -> bool:
    return bool(p.get("tested"))


def _prop_line(p: dict) -> str:
    s = f"{p.get('attr') or p['name']}  {p['type']}{' ro' if p.get('readonly') else ''}"
    if p.get("values"):
        s += f"  [{_vals(p)}]"
    if p.get("default") not in (None, "") and p["name"] != "NAME":
        s += f"  default={p['default']}"
    c = p.get("corpus")
    if c:
        s += f"  | MA3 uses it {c['n']}x: " + ", ".join(f"{v!r}" for v, _ in c["top"][:4])
    if _confirmed(p):
        t = p["tested"]
        e = t.get("effect")
        s += "  | tested: no visible effect" if e == "none" else f"  | tested: {e}" if e else "  | tested notes"
    return s


def ui_classes(query: str = "", category: str = "", attr: str = "", limit: int = 60, version: str = "") -> dict:
    ver = _cver(version)
    C = _classes(ver)
    rows = []
    for name, r in C.items():
        if query and query.lower() not in name.lower():
            continue
        if category and r.get("category") != category:
            continue
        if attr:
            a = attr.upper()
            if not any(p["name"] == a for p in r["props"]) and not any(e["name"] == a for e in r["events"]) \
                    and not any(x["attr"].upper() == a for x in r.get("import_only_attrs", [])):
                continue
        k = r.get("corpus") or {}
        rows.append({"class": name, "category": r.get("category"), "base": (r.get("ancestors") or [None])[0],
                     "uses": k.get("uses", 0), "files": k.get("files", 0), "props": len(r["props"]),
                     **({"crashes": True} if r.get("crashes") else {}),
                     **({"tested": len((r["tested"].get("images") or [])) or True} if r.get("tested") else {}),
                     **({"live": False} if not r["props"] else {})})
    rows.sort(key=lambda x: -x["uses"])
    cats: dict[str, int] = {}
    for r in C.values():
        cats[r.get("category", "other")] = cats.get(r.get("category", "other"), 0) + 1
    return {"version": ver, "total": len(rows), "classes": rows[:limit],
            **({"categories": cats} if not (query or category or attr) else {}),
            "note": "uses/files = how often MA3's own .uixml uses the tag; props = live property count (0 = not read "
                    "live yet); tested = hand-tested notes exist (a number = that many probe images).",
            "next": "ui_class(name) for its properties, events, nesting and examples"}


def ui_class(name: str, prop: str = "", show: str = "", image: str = "", version: str = "") -> tuple[dict, bytes | None]:
    ver = _cver(version)
    C = _classes(ver)
    r = C.get(name) or next((v for k, v in C.items() if k.lower() == name.lower()), None)
    if not r:
        close = [k for k in C if name.lower() in k.lower()][:15]
        return {"error": f"no UI class {name!r}", "did_you_mean": close}, None
    if image:
        return _class_image(ver, r, image)
    return _ui_class(ver, r, C, prop, show, version), None


def _class_image(ver: str, r: dict, image: str) -> tuple[dict, bytes | None]:
    imgs = (r.get("tested") or {}).get("images") or []
    i = next((x for x in imgs if x["file"] == image or x["file"].rsplit(".", 1)[0] == image), None)         or next((x for x in imgs if image.lower() in x["file"].lower()), None)
    if not i:
        return {"error": f"{r['class']} has no probe image {image!r}", "images": [x["file"] for x in imgs]}, None
    p = UI_ROOT / ver / "images" / r["class"] / i["file"]
    if not p.is_file():
        return {"error": f"image file missing on the server: {p}"}, None
    img = _load_png(p)
    info = {"class": r["class"], "image": i["file"], "caption": i["caption"], "source": i.get("source"),
            "probed_on": i.get("ma3"), "size": f"{img.width}x{img.height}"}
    return info, _png_bytes(img, 0.0)


def _ui_class(ver: str, r: dict, C: dict, prop: str, show: str, version: str) -> dict:
    k = r.get("corpus") or {}
    if prop:
        pu = prop.upper()
        p = next((x for x in r["props"] if x["name"] == pu or (x.get("attr") or "").upper() == pu), None)
        e = next((x for x in r["events"] if x["name"] == pu), None)
        if not p and not e:
            return {"error": f"{r['class']} has no property {prop!r}",
                    "did_you_mean": [x.get("attr") or x["name"] for x in r["props"] if pu in x["name"]][:15]}
        out = dict(p or e)
        if out.get("enum") == "YesNo":
            out["values"] = ["Yes", "No"]
        elif out.get("values"):
            out["values"] = [v for v in out["values"] if v.split("=")[0].isprintable()]
        if out.get("tested") and not _confirmed(out):
            out.pop("tested")
        out["class"] = r["class"]
        out["accepted_by"] = sorted(c for c, v in C.items() if any(x["name"] == (p or e)["name"] for x in v["props"] + v["events"]))[:60]
        return out
    own = [p for p in r["props"] if p.get("own")]
    used = [p for p in r["props"] if not p.get("own") and (p.get("corpus") or _confirmed(p))]
    rest = [p for p in r["props"] if p not in own and p not in used]
    info = {
        "version": ver, "class": r["class"], "category": r.get("category"),
        "inherits": " > ".join([r["class"]] + r.get("ancestors", [])),
        **({"subclasses": r["subclasses"][:30]} if r.get("subclasses") else {}),
        "live": r.get("live"),
        **({"WARNING": r["crashes"]} if r.get("crashes") else {}),
        "used_in_ma3": {"uses": k.get("uses", 0), "files": k.get("files", 0),
                        "parents": dict(list((k.get("parents") or {}).items())[:8]),
                        "children": dict(list((k.get("children") or {}).items())[:12])} if k else None,
        "properties_own": [_prop_line(p) for p in own],
        "properties_used_by_ma3": [_prop_line(p) for p in sorted(used, key=lambda p: -(p.get("corpus") or {}).get("n", 0))],
        "events": [f"{e['attr']}" + (f"  | MA3 uses it {e['corpus']['n']}x: {e['corpus']['top'][0][0]!r}" if e.get("corpus") else "")
                   for e in r["events"]],
        **({"attrs_not_live_properties": [f"{a['attr']} ({a['n']}x: {', '.join(repr(v) for v, _ in a['top'][:3])})"
                                          for a in r["import_only_attrs"]]} if r.get("import_only_attrs") else {}),
        **({"attributes_ma3_sets (not read live: no types/defaults)": [
            f"{a['attr']} ({a['n']}x: {', '.join(repr(v) for v, _ in a['top'][:4])})" for a in r["corpus_attrs"]]}
           if r.get("corpus_attrs") else {}),
        "examples": [f"{e['file']}:{e['line']}  {e['text']}" for e in (k.get("examples") or [])[:4]],
    }
    if show == "all":
        info["properties_inherited"] = [_prop_line(p) for p in rest]
    else:
        info["properties_inherited"] = f"{len(rest)} more inherited, not used by MA3 on this class -- show='all' lists them"
    if r.get("nesting"):
        info["nesting"] = r["nesting"]
    t = r.get("tested")
    if t:
        info["tested"] = {
            **({"primitive": t["primitive"]} if t.get("primitive") else {}),
            **({"notes": t["notes"][:3000]} if t.get("notes") else {}),
            **({"images": [f"ui_class({r['class']!r}, image={x['file']!r})  -- {x['caption'][:160]}"
                           for x in t.get("images") or []]} if t.get("images") else {}),
            "probed_on": "grandMA3 onPC 2.4.2",
        }
    # what it looks like: instances on the reference screenshots
    try:
        cat = _catalog(_ver(version))
        seen = []
        for mname, si, w in cat["widgets"]:
            if w["class"] == r["class"] and not w.get("hidden") and w["rect"][2] > 8 and w["rect"][3] > 8:
                seen.append((0 if (w.get("text") or "").strip() else 1, mname, si, w))
        seen.sort(key=lambda x: x[0])
        looks, used_menus = [], set()
        for _, mname, si, w in seen:
            if mname in used_menus:
                continue
            used_menus.add(mname)
            st = cat["menus"][mname]["states"][si]["state"]
            looks.append(f"ui_view(menu={mname!r}, state={st!r}, widget={w['id']})  -- {w.get('text') or w.get('name')!r}")
            if len(looks) >= 4:
                break
        info["looks_like"] = looks or "no instance on the reference screenshots"
    except FileNotFoundError:
        pass
    info["next"] = "ui_class(name, prop=<attr>) for one property in full (all values, every class that accepts it)"
    return info


# ---------------------------------------------------------------- lib_menus corpus
@lru_cache(maxsize=4)
def _corpus(ver: str) -> list[tuple[str, list[str]]]:
    root = UI_ROOT / ver / "lib_menus"
    out = []
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() in (".uixml", ".xml", ".lua") and p.is_file():
            out.append((str(p.relative_to(root)).replace("\\", "/"),
                        p.read_text(encoding="utf-8", errors="replace").lstrip("﻿").splitlines()))
    return out


def search_uixml(query: str, limit: int = 30, files: str = "uixml", version: str = "") -> dict:
    """Case-insensitive search over lib_menus. Several words = all on the same line. Regex with re: prefix."""
    ver = _ver(version)
    exts = {"uixml": (".uixml",), "lua": (".lua",), "xml": (".xml",), "all": (".uixml", ".lua", ".xml")}.get(files, (".uixml",))
    if query.startswith("re:"):
        rx = re.compile(query[3:], re.I)
        match = lambda s: rx.search(s) is not None  # noqa: E731
    else:
        ts = _terms(query)
        match = lambda s: all(t in s.lower() for t in ts)  # noqa: E731
    hits, total, per_file = [], 0, {}
    for rel, lines in _corpus(ver):
        if not rel.endswith(exts):
            continue
        for i, l in enumerate(lines, 1):
            if match(l):
                total += 1
                per_file[rel] = per_file.get(rel, 0) + 1
                if len(hits) < limit:
                    hits.append({"file": rel, "line": i, "text": l.strip()[:220]})
    return {"version": ver, "query": query, "total": total, "files": len(per_file), "hits": hits,
            "next": "ui_xml(file, line) shows the element in context and where it appears on screen"}


# ---------------------------------------------------------------- colours
@lru_cache(maxsize=4)
def _colors(ver: str) -> dict:
    root = ET.parse(UI_ROOT / ver / "lib_color_themes" / "default.xml").getroot()
    reg = {}
    for g in root.iter("ColorDefGroup"):
        for c in g.findall("ColorDef"):
            reg[f"{g.attrib.get('Name')}.{c.attrib.get('Name')}"] = {"rgba": c.attrib.get("RGBA"), "ref": None}
    for g in root.iter("ColorGroup"):
        for c in g.findall("Color"):
            reg[f"{g.attrib.get('Name')}.{c.attrib.get('Name')}"] = {"rgba": c.attrib.get("RGBA"), "ref": c.attrib.get("ColorDefRef")}
    return reg


def _resolve_rgba(reg: dict, key: str, depth: int = 0) -> tuple[str | None, list[str]]:
    if depth > 6 or key not in reg:
        return None, []
    rec = reg[key]
    if rec["rgba"]:
        return rec["rgba"], [key]
    if rec["ref"] and rec["ref"] != key:
        rgba, chain = _resolve_rgba(reg, rec["ref"], depth + 1)
        return rgba, [key] + chain
    return None, [key]


def resolve_color(token: str, limit: int = 40, version: str = "") -> tuple[dict, bytes | None]:
    ver = _ver(version)
    reg = _colors(ver)
    key = next((k for k in reg if k.lower() == token.lower()), None)
    if key:
        rgba, chain = _resolve_rgba(reg, key)
        info = {"version": ver, "token": key, "rgba": rgba, "chain": chain,
                "note": None if rgba else "no static colour -- computed at runtime"}
        data = None
        if rgba:
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (96, 48), (255, 255, 255, 255))
            d = ImageDraw.Draw(img)
            for yy in range(0, 48, 8):
                for xx in range(0, 96, 8):
                    if (xx // 8 + yy // 8) % 2:
                        d.rectangle([xx, yy, xx + 7, yy + 7], fill=(200, 200, 200, 255))
            r, g, b, a = (int(rgba[i:i + 2], 16) for i in (0, 2, 4, 6))
            img.alpha_composite(Image.new("RGBA", (96, 48), (r, g, b, a)))
            data = _png_bytes(img, 1.0)
        return info, data
    ts = _terms(token)
    hits = [k for k in sorted(reg, key=str.lower) if all(t in k.lower() for t in ts)]
    return {"version": ver, "query": token, "total": len(hits),
            "hits": [{"token": k, "rgba": _resolve_rgba(reg, k)[0]} for k in hits[:limit]],
            "next": "resolve_color(<exact token>) for the chain and a swatch"}, None


# ---------------------------------------------------------------- icons / textures
TEX_KIND = {"Texture": "icon", "SecureTexture": "icon (secure)", "Backdrop": "backdrop (9-slice corner/frame)"}


@lru_cache(maxsize=4)
def _textures(ver: str) -> dict:
    root = ET.parse(UI_ROOT / ver / "textures" / "graphics.textures.xml").getroot()
    reg = {}
    for el in root.iter():
        if el.tag in TEX_KIND and el.attrib.get("Name"):
            reg[el.attrib["Name"]] = {"kind": TEX_KIND[el.tag], **{k.lower(): v for k, v in el.attrib.items() if k != "Name"}}
    return reg


def resolve_icon(name: str, limit: int = 40, version: str = "") -> tuple[dict, bytes | None]:
    ver = _ver(version)
    reg = _textures(ver)
    key = next((k for k in reg if k.lower() == name.lower()), None)
    if key:
        rec = reg[key]
        info = {"version": ver, "name": key, **rec}
        data = None
        fn = rec.get("filename")
        p = UI_ROOT / ver / "textures" / fn if fn else None
        if p and p.is_file():
            from PIL import Image
            img = Image.open(p).convert("RGBA")
            info["size"] = list(img.size)
            bg = Image.new("RGBA", img.size, (40, 40, 44, 255))
            bg.alpha_composite(img)
            data = _png_bytes(bg, 1.0 if max(img.size) >= 64 else 64 / max(img.size))
        elif fn:
            info["note"] = f"{fn} is not in the textures copy"
        return info, data
    ts = _terms(name)
    hits = [k for k in sorted(reg, key=str.lower) if all(t in k.lower() or t in (reg[k].get("filename") or "").lower() for t in ts)]
    return {"version": ver, "query": name, "total": len(hits),
            "hits": [{"name": k, "kind": reg[k]["kind"], "file": reg[k].get("filename")} for k in hits[:limit]],
            "next": "resolve_icon(<exact name>) renders it"}, None
