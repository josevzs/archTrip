"""Photos and drawings for a landmark, from Wikidata + Wikimedia Commons (free, no key).

Pipeline: Wikidata entity search (name) -> pick the hit that sits near the landmark
-> P18 image, P3311 plan images, P373 Commons category, P625 coordinates, Wikipedia
sitelink -> Commons category files (photos) + an in-category search for drawings.
Falls back to a plain Commons search when Wikidata has nothing."""
import os
import re
import threading
import time
from urllib.parse import quote

import requests

from .geo import haversine_km

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia's policy wants a contact in the User-Agent; override with ARCHTRIP_CONTACT.
USER_AGENT = "archTrip/0.1 (%s)" % os.environ.get("ARCHTRIP_CONTACT", "https://github.com/josevzs/archTrip; herramienta docente")
TIMEOUT = 15
MIN_INTERVAL = 1.0          # anonymous clients get 429s well below this
MAX_PHOTOS = 8
MAX_DRAWINGS = 6
THUMB_WIDTH = 640
LARGE_WIDTH = 1600
NEAR_KM = 60                # a Wikidata hit must sit this close to the landmark's known position

DRAWING_WORDS = re.compile(
    r"\b(plan|plans|section|elevation|drawing|sketch|diagram|floor ?plan|layout|axonometr\w*|"
    r"isometr\w*|blueprint|grundriss|schnitt|ansicht|planta|secci[oó]n|alzado|plano|croquis)\b"
    r"|平面|断面|立面|図面|配置|見取", re.I)
BAD_HIT = re.compile(r"exhibition|album|song|single|film|novel|painting|metro station|railway station|"
                     r"subway station|train station|disambiguation|family name|given name|surname", re.I)
BUILDING_WORDS = re.compile(
    r"building|museum|temple|shrine|church|cathedral|tower|station|hall|house|villa|castle|library|"
    r"theat(re|er)|stadium|gymnasium|arena|park|garden|hotel|store|shop|school|university|college|"
    r"skyscraper|architect|structure|bridge|palace|complex|cent(re|er)|gallery|monastery|pagoda|"
    r"residence|mansion|apartment|office|headquarters|terminal|airport|market|plaza|pavilion|"
    r"chapel|mosque|synagogue|monument|memorial|kindergarten|factory|mill|warehouse|bank|club|"
    r"district|quarter|village|neighbo(u)?rhood|street|site|ruins|observatory|planetarium|aquarium", re.I)

_lock = threading.Lock()
_last = 0.0


def _throttle():
    global _last
    with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()


def _get(url, params):
    params = dict(params, format="json")
    for attempt in (1, 2):
        _throttle()
        resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        if resp.status_code == 429 and attempt == 1:
            time.sleep(min(int(resp.headers.get("Retry-After", "5") or 5), 30))
            continue
        resp.raise_for_status()
        return resp.json()


def file_url(filename, width=None):
    """Hotlinkable URL for a Commons file by name (redirects to the actual file/thumb)."""
    u = "https://commons.wikimedia.org/wiki/Special:FilePath/" + quote(filename.replace(" ", "_"))
    return u + (f"?width={width}" if width else "")


def _claim_values(claims, prop):
    out = []
    for c in claims.get(prop, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if v is not None:
            out.append(v)
    return out


def _entity_info(qid, entity):
    claims = entity.get("claims", {})
    coords = _claim_values(claims, "P625")
    sitelinks = entity.get("sitelinks", {})
    wiki = None
    for lang in ("es", "en", "ja"):
        sl = sitelinks.get(f"{lang}wiki")
        if sl:
            wiki = f"https://{lang}.wikipedia.org/wiki/" + quote(sl["title"].replace(" ", "_"))
            break
    return {
        "id": qid,
        "image": next(iter(_claim_values(claims, "P18")), None),
        "plans": _claim_values(claims, "P3311"),
        "category": next(iter(_claim_values(claims, "P373")), None),
        "lat": coords[0]["latitude"] if coords else None,
        "lon": coords[0]["longitude"] if coords else None,
        "wikipedia": wiki,
    }


GENERIC_WORDS = {"museo", "museum", "de", "del", "of", "the", "la", "el", "los", "las", "casa", "house", "edificio",
                 "building", "iglesia", "church", "templo", "temple", "centro", "center", "centre", "arte", "art",
                 "contemporáneo", "contemporary", "moderno", "modern", "nacional", "national", "prefectural",
                 "city", "municipal", "and", "y", "e", "memorial", "hall", "library", "biblioteca", "museo", "galería",
                 "gallery", "villa", "palacio", "palace", "castillo", "castle", "torre", "tower", "in", "en", "at"}


def core_name(name):
    """'Museo de Serralves' -> 'Serralves': the distinctive part, for a last-resort label search."""
    base = re.sub(r"\s*\([^)]*\)", "", name).split(",")[0]
    words = [w for w in re.split(r"\s+", base.strip()) if w and w.lower().strip("'’") not in GENERIC_WORDS]
    core = " ".join(words)
    return core if core and core.lower() != base.strip().lower() else None


def _search_labels(query, lang):
    data = _get(WIKIDATA_API, {"action": "wbsearchentities", "search": query, "language": lang,
                               "uselang": "en", "type": "item", "limit": 5})
    return [{"id": h["id"], "description": h.get("description", "") or ""} for h in data.get("search", [])]


def _search_fulltext(query):
    data = _get(WIKIDATA_API, {"action": "query", "list": "search", "srsearch": query, "srlimit": 5})
    return [{"id": h["title"], "description": re.sub(r"<[^>]+>", "", h.get("snippet", "") or "")}
            for h in data.get("query", {}).get("search", []) if h.get("title", "").startswith("Q")]


def _pick(hits, lat, lon, strict):
    """Choose the hit that is geographically plausible and looks like a place."""
    hits = [h for h in hits if not BAD_HIT.search(h["description"])][:4]
    if not hits:
        return None
    ents = _get(WIKIDATA_API, {"action": "wbgetentities", "ids": "|".join(h["id"] for h in hits),
                               "props": "claims|sitelinks", "sitefilter": "enwiki|eswiki|jawiki"}).get("entities", {})
    infos = [(h, _entity_info(h["id"], ents.get(h["id"], {}))) for h in hits]
    near = lambda info: info["lat"] is not None and lat is not None and haversine_km(lat, lon, info["lat"], info["lon"]) <= NEAR_KM
    placey = lambda h: bool(BUILDING_WORDS.search(h["description"]))
    if lat is not None:   # an entity that sits far from where the landmark is cannot be it (namesakes abroad)
        infos = [(h, info) for h, info in infos if info["lat"] is None or near(info)]
    if strict:   # last resort: must be near AND described as a place
        for h, info in infos:
            if near(info) and placey(h):
                return info
        return None
    for h, info in infos:      # 1) near where we think the landmark is
        if near(info):
            return info
    for h, info in infos:      # 2) described as a place
        if placey(h):
            return info
    for h, info in infos:      # 3) something with pictures and no coordinates to contradict
        if info["lat"] is None and (info["image"] or info["category"]):
            return info
    return None


def wikidata_lookup(name, lat=None, lon=None):
    """-> entity info dict or None. Label search (English, then Spanish), then full-text
    search, then the distinctive part of the name with strict checks."""
    for lang in ("en", "es"):   # professors write names in Spanish or English
        info = _pick(_search_labels(name, lang), lat, lon, strict=False)
        if info:
            return info
    info = _pick(_search_fulltext(name), lat, lon, strict=False)
    if info:
        return info
    core = core_name(name)
    if core:
        return _pick(_search_labels(core, "en"), lat, lon, strict=True)
    return None


def _files_from_pages(pages):
    out = []
    for p in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        ii = (p.get("imageinfo") or [{}])[0]
        mime = ii.get("mime", "")
        if not mime.startswith("image/") or "thumburl" not in ii:
            continue
        title = p["title"].split(":", 1)[-1]
        out.append({
            "title": title,
            "mime": mime,
            "thumb": ii["thumburl"],
            "url": file_url(title, LARGE_WIDTH),   # a resized copy, never the multi-MB original
            "page_url": ii.get("descriptionurl") or "https://commons.wikimedia.org/wiki/" + quote(p["title"].replace(" ", "_")),
        })
    return out


def commons_category_files(category, limit=12):
    data = _get(COMMONS_API, {"action": "query", "generator": "categorymembers", "gcmtitle": "Category:" + category,
                              "gcmtype": "file", "gcmlimit": limit, "prop": "imageinfo",
                              "iiprop": "url|mime", "iiurlwidth": THUMB_WIDTH})
    return _files_from_pages(data.get("query", {}).get("pages", {}))


def commons_search(query, limit=10):
    data = _get(COMMONS_API, {"action": "query", "generator": "search", "gsrsearch": query, "gsrnamespace": 6,
                              "gsrlimit": limit, "prop": "imageinfo", "iiprop": "url|mime",
                              "iiurlwidth": THUMB_WIDTH})
    return _files_from_pages(data.get("query", {}).get("pages", {}))


def commons_drawings(category, limit=8):
    q = (f'incategory:"{category}" (plan OR section OR elevation OR drawing OR sketch OR diagram OR '
         f'Grundriss OR Schnitt OR planta OR sección OR alzado OR 平面図 OR 断面図 OR 立面図 OR 図面)')
    return commons_search(q, limit)


DRAWING_MIMES = {"image/png", "image/svg+xml", "image/gif"}


def is_drawing(title, mime=None):
    """Drawings are named as such, or are line art (png/svg) rather than photos (jpeg)."""
    return bool(DRAWING_WORDS.search(title)) or (mime in DRAWING_MIMES and not re.search(r"photo|foto", title, re.I))


def fetch_images(name, city, lat=None, lon=None):
    """-> {wikidata_id, wikipedia_url, lat, lon, images: [{kind, url, thumb, title, page_url, source}]}
    Raises requests.RequestException on network trouble (caller decides whether to retry)."""
    result = {"wikidata_id": None, "wikipedia_url": None, "lat": None, "lon": None, "images": []}
    photos, drawings, seen = [], [], set()

    def add(item, kind, source):
        key = item["title"].lower()
        if key in seen:
            return
        seen.add(key)
        (drawings if kind == "plano" else photos).append(dict(item, kind=kind, source=source))

    wd = wikidata_lookup(name, lat, lon)
    if wd:
        result.update({"wikidata_id": wd["id"], "wikipedia_url": wd["wikipedia"], "lat": wd["lat"], "lon": wd["lon"]})
        if wd["image"]:
            add({"title": wd["image"], "url": file_url(wd["image"], LARGE_WIDTH), "thumb": file_url(wd["image"], THUMB_WIDTH),
                 "page_url": "https://commons.wikimedia.org/wiki/File:" + quote(wd["image"].replace(" ", "_"))}, "foto", "wikidata")
        for pl in wd["plans"]:
            add({"title": pl, "url": file_url(pl, LARGE_WIDTH), "thumb": file_url(pl, THUMB_WIDTH),
                 "page_url": "https://commons.wikimedia.org/wiki/File:" + quote(pl.replace(" ", "_"))}, "plano", "wikidata")
        if wd["category"]:
            for f in commons_category_files(wd["category"]):
                add(f, "plano" if is_drawing(f["title"], f.get("mime")) else "foto", "commons")
            for f in commons_drawings(wd["category"]):   # matched on description text: re-check the file itself
                add(f, "plano" if is_drawing(f["title"], f.get("mime")) else "foto", "commons")
    if not photos and not drawings:
        for f in commons_search(f"{name} {city}"):
            add(f, "plano" if is_drawing(f["title"], f.get("mime")) else "foto", "commons")

    result["images"] = [{k: v for k, v in im.items() if k != "mime"} for im in photos[:MAX_PHOTOS] + drawings[:MAX_DRAWINGS]]
    return result
