"""Does the landmark have a page on ArchDaily / Arquitectura Viva? Resolve the direct link
so the UI only offers buttons that lead somewhere. Both sites expose the JSON their own
search pages use; we call it sparingly (1 req/s) and match titles strictly enough to
prefer a missing link over a wrong one."""
import re
import threading
import time
import unicodedata

import requests

from .images import USER_AGENT

AD_API = "https://www.archdaily.com/search/api/v1/us/{kind}"
AV_API = "https://arquitecturaviva.com/listados/obtener_listado_ajax/buscador/es/{page}"
AV_TAGS = "https://arquitecturaviva.com/listados/obtener_tags/0/0/es/buscador"
AV_SITE = "https://arquitecturaviva.com"
AV_MAX_PAGES = 3
TIMEOUT = 15
MIN_INTERVAL = 1.0
STOP = {"de", "del", "la", "el", "los", "las", "y", "e", "a", "en", "the", "of", "and", "in", "at", "da", "do", "dos",
        "das", "no", "na", "&", "/", "-", "i", "ii", "iii", "un", "una", "le", "les", "der", "die", "das"}
# words a result title may add without pointing at a different building
GENERIC = {"museum", "museo", "glass", "house", "casa", "store", "shop", "boutique", "flagship", "building", "edificio",
           "center", "centre", "centro", "hall", "station", "estacion", "library", "biblioteca", "gallery", "galeria",
           "art", "arte", "tower", "torre", "park", "parque", "plaza", "school", "escuela", "church", "iglesia", "hotel",
           "office", "oficinas", "villa", "pavilion", "pabellon", "project", "proyecto", "design", "architecture",
           "arquitectura", "new", "nuevo", "nueva", "renovation", "extension", "ampliacion", "interior", "main", "japan",
           "japon", "tokyo", "tokio", "kyoto", "kioto", "osaka", "ad", "classics", "classic", "clasicos", "clasico",
           "review", "critica", "construccion", "sede", "teatro", "theatre", "theater", "auditorio", "auditorium",
           "universidad", "university", "tienda", "vivienda", "viviendas", "apartamentos", "apartments", "memorial",
           "cultural", "cultura", "contemporaneo", "contemporary", "moderno", "modern", "nacional", "national",
           "prefectural", "city", "ciudad", "municipal", "temple", "templo", "shrine", "santuario", "castle", "castillo"}
ARCHITECT_STOP = {"associates", "partners", "architects", "architect", "arquitectos", "studio", "office", "and", "co",
                  "ltd", "inc", "tradicional", "vernacula", "meiji", "edo", "taisho", "showa", "heian", "varios",
                  "entreguerras", "posguerra", "siglo", "reconstruccion", "reconstrucciones", "jardines", "jardin",
                  "escuela", "moderna", "ampliaciones", "ampliacion", "anexo", "fachada", "podio", "renovacion",
                  "cupulas", "gassho", "zukuri", "shoin", "zen", "samurai", "ageya", "machiya", "hotelera", "industrial",
                  "patrimonio", "infraestructura", "vial", "convertida", "paisaje", "incluye", "casa", "otros"}

_lock = threading.Lock()
_last = 0.0
_av_tag_cache = {}     # architect key -> (tag id or None)
_av_works_cache = {}   # tag id -> [items]


def _throttle():
    global _last
    with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()


def _ascii(text):
    t = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in t if not unicodedata.combining(c))


def _query(text):
    """Plain words for a site search: no accents, no punctuation (AV's backend 500s on apostrophes)."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s-]", " ", _ascii(text))).strip()


def tolerant(fn, *args):
    """Run a lookup; a broken query on the site's side (HTTP 4xx/5xx) means 'no link', while
    rate limiting and network trouble propagate so the step is retried later."""
    try:
        return fn(*args)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            raise
        return None


def tokens(text):
    return {w for w in re.split(r"[^a-z0-9]+", _ascii(text).lower()) if len(w) > 1 and w not in STOP}


def architect_words(architect):
    """Distinctive words of the architect field: 'Rem Koolhaas / OMA' -> {'rem','koolhaas','oma'}.
    Descriptors used for anonymous works ('Tradicional · s. XVII') yield nothing."""
    return {w for w in tokens(architect) if len(w) >= 3 and w not in ARCHITECT_STOP and not re.fullmatch(r"[ivxlcdm]+", w)}


# Spanish spellings the professors use -> what the sites print
PLACE_ALIASES = {"oporto": "porto", "tokio": "tokyo", "kioto": "kyoto", "japon": "japan", "espana": "spain",
                 "francia": "france", "italia": "italy", "alemania": "germany", "suiza": "switzerland", "lisboa": "lisbon",
                 "londres": "london", "sevilla": "seville", "florencia": "florence", "venecia": "venice", "roma": "rome",
                 "viena": "vienna", "praga": "prague", "copenhague": "copenhagen", "estocolmo": "stockholm",
                 "atenas": "athens", "pekin": "beijing", "seul": "seoul", "belgica": "belgium", "grecia": "greece",
                 "irlanda": "ireland", "dinamarca": "denmark", "noruega": "norway", "suecia": "sweden",
                 "finlandia": "finland", "polonia": "poland", "austria": "austria", "brasil": "brazil",
                 "mexico": "mexico", "estados unidos": "usa", "eeuu": "usa", "china": "china", "corea": "korea"}


def place_tokens(*places):
    out = set()
    for pl in places:
        for w in tokens(pl):
            out.add(w)
            if w in PLACE_ALIASES:
                out |= tokens(PLACE_ALIASES[w])
    return out


def location_ok(location, city, country=""):
    """True/False when the result says where it is; None when it doesn't."""
    loc = tokens(location)
    if not loc:
        return None
    return bool(loc & place_tokens(city, country))


def matches(title, name, architect, extra="", allow=(), architect_known=False):
    """True when the result is about this building: the title covers the landmark's name and
    adds nothing distinctive of its own (or the architect confirms it). `extra` is any other
    text of the result (offices, subtitle); `allow` extra words that are fine (the city);
    `architect_known` when the result set was already restricted to the architect."""
    want = tokens(name)
    if not want:
        return False
    head = tokens(title.split("/")[0])          # "Name / Office": the name part
    have = head | tokens(title) | tokens(extra)
    coverage = len(want & have) / len(want)
    arch = architect_known or any(w in have for w in architect_words(architect))
    distinctive_extras = head - want - GENERIC - set(tokens(" ".join(allow)))
    if coverage >= 0.99:
        return not distinctive_extras or arch
    return coverage >= 0.5 and arch and not distinctive_extras


# ---------------------------------------------------------------- archdaily

def _ad_search(kind, query):
    _throttle()
    resp = requests.get(AD_API.format(kind=kind), params={"q": query},
                        headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("results") or []


def find_archdaily(name, architect, city="", country=""):
    """-> URL of the project page (or an article such as an AD Classic), or None."""
    words = architect_words(architect)
    # the site's search likes the full architect name; the bare name is the fallback
    queries = [_query(f"{name} {architect}")] if words else []
    queries.append(_query(name))
    for kind, query in [(k, q) for k in ("projects", "articles") for q in queries]:
        confirmed, unconfirmed = [], []
        for r in _ad_search(kind, query)[:10]:
            offices = " ".join(o.get("name", "") for o in (r.get("offices") or []) if isinstance(o, dict))
            title = r.get("title", "")
            if not matches(title, name, architect, offices, allow=[city]):
                continue
            url = (r.get("url") or "").split("?")[0]
            if not url:
                continue
            have = tokens(title) | tokens(offices)
            if any(w in have for w in architect_words(architect)):
                confirmed.append(url)
            elif location_ok(r.get("location", ""), city, country) is not False:   # a namesake abroad is not it
                unconfirmed.append(url)
        if confirmed:
            return confirmed[0]
        if len(unconfirmed) == 1:      # a second plausible candidate would mean we can't tell
            return unconfirmed[0]
    return None


# --------------------------------------------------------- arquitectura viva

_AV_ITEM = re.compile(r'data-titulo="([^"]*)"[^>]*data-link="([^"]*)"[^>]*data-entidad="([^"]*)"')


def _av_post(page, form):
    _throttle()
    data = {"form[entidad]": "buscador", "form[lang]": "es", "form[pagina]": page,
            "form[filtros][]": ["obras", "articulos"]}
    data.update(form)
    resp = requests.post(AV_API.format(page=page), params={"nc": int(time.time())}, data=data,
                         headers={"User-Agent": USER_AGENT, "X-Requested-With": "XMLHttpRequest"}, timeout=TIMEOUT)
    resp.raise_for_status()
    d = resp.json()
    items = [{"title": m.group(1), "url": m.group(2), "kind": m.group(3)} for m in _AV_ITEM.finditer(d.get("items") or "")]
    return items, int(d.get("cantidad_total") or 0)


def _av_tag(architect):
    """AV's autocomplete gives the tag id of an architect ('Rem Koolhaas' -> '50045')."""
    words = architect_words(architect)
    if not words:
        return None
    key = " ".join(sorted(words))
    if key in _av_tag_cache:
        return _av_tag_cache[key]
    _throttle()
    resp = requests.get(AV_TAGS, params={"search": sorted(words, key=len)[-1]},
                        headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    tag, surname = None, sorted(words, key=len)[-1]
    for opt in resp.json() or []:
        label = re.sub(r"<[^>]+>", " ", opt.get("text", ""))
        if "Arquitecto" in label and surname in tokens(label):
            tag = str(opt.get("value"))
            break
    _av_tag_cache[key] = tag
    return tag


def _av_works(tag):
    if tag in _av_works_cache:
        return _av_works_cache[tag]
    items, page = [], 1
    while page <= AV_MAX_PAGES:
        got, total = _av_post(page, {"form[tags]": tag, "form[filtros][]": ["obras"]})
        items += got
        if not got or len(items) >= total:
            break
        page += 1
    _av_works_cache[tag] = items
    return items


def find_av(name, architect, city=""):
    """-> URL on arquitecturaviva.com: the building among the architect's works, else a
    free-text hit whose title is unmistakably the building. None when unsure."""
    tag = _av_tag(architect)
    if tag:
        for it in _av_works(tag):
            if matches(it["title"], name, architect, allow=[city], architect_known=True):
                return AV_SITE + it["url"] if it["url"].startswith("/") else it["url"]
    items, _ = _av_post(1, {"form[buscar]": _query(name)})
    for kind in ("obras", "articulos"):
        for it in items:
            if it["kind"] == kind and matches(it["title"], name, architect, allow=[city]):
                return AV_SITE + it["url"] if it["url"].startswith("/") else it["url"]
    return None
