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

import unicodedata

import requests

from .geo import haversine_km

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_API = "https://%s.wikipedia.org/w/api.php"
WIKI_KM = 25                        # buscando por texto hay que apretar más que en Wikidata
WIKI_LANGS = ("es", "en", "ja")      # el profesor escribe en español; el edificio suele estar en ja
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia's policy wants a contact in the User-Agent; override with ARCHTRIP_CONTACT.
USER_AGENT = "archTrip/0.1 (%s)" % os.environ.get("ARCHTRIP_CONTACT", "https://github.com/josevzs/archTrip; herramienta docente")
TIMEOUT = 15
MIN_INTERVAL = 1.0          # anonymous clients get 429s well below this
MAX_PHOTOS = 8
MAX_DRAWINGS = 6
THUMB_WIDTH = 640
LARGE_WIDTH = 1600
NEAR_KM = 30                # a Wikidata hit must sit this close to the landmark: más lejos ya es
                            # otro sitio con el mismo nombre (el Kōmyō-in de Sakai, no el de Kioto)'s known position

DRAWING_WORDS = re.compile(
    r"\b(plan|plans|section|elevation|drawing|sketch|diagram|floor ?plan|layout|axonometr\w*|"
    r"isometr\w*|blueprint|grundriss|schnitt|ansicht|planta|secci[oó]n|alzado|plano|croquis)\b"
    r"|平面|断面|立面|図面|配置|見取", re.I)
# descripciones que delatan que la ficha no es un edificio: obras, publicaciones, conceptos
BAD_HIT = re.compile(r"exhibition|album|song|single|film|novel|painting|metro station|railway station|"
                     r"subway station|train station|disambiguation|family name|given name|surname|"
                     r"magazine|periodical|journal|newspaper|encyclopedia|manga|anime|video game|"
                     r"company|corporation|manufacturer|brand|empresa|compa[ñn][íi]a|marca|fabricante|"
                     r"concept|term for|unit of|type of", re.I)
# oficios y cargos: descartan la ficha solo si además no suena a edificio, porque la descripción
# de un edificio suele nombrar a su autor ("villa by architect X")
# en inglés y en español: la Wikipedia en español es la primera que se consulta
PERSON_WORDS = re.compile(r"\b(architect|emperor|empress|politician|writer|poet|novelist|photographer|"
                          r"monk|samurai|actor|actress|musician|composer|painter|designer|scientist|"
                          r"engineer|businessman|daimyo|shogun|"
                          r"arquitecto|emperador|emperatriz|pol[ií]tic[oa]|escritor|poeta|novelista|"
                          r"fot[óo]grafo|monje|samur[áa]i|actriz|m[úu]sico|compositor|pintor|"
                          r"dise[ñn]ador|cient[íi]fic[oa]|ingeniero|cal[íi]grafo|empresario)\w*", re.I)
BUILDING_WORDS = re.compile(
    r"building|museum|temple|shrine|church|cathedral|tower|station|hall|house|villa|castle|library|"
    r"theat(re|er)|stadium|gymnasium|arena|park|garden|hotel|store|shop|school|university|college|"
    r"skyscraper|architectur\w*|structure|bridge|palace|complex|cent(re|er)|gallery|monastery|pagoda|"
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
    return [{"id": h["id"], "label": h.get("label", "") or "", "description": h.get("description", "") or ""}
            for h in data.get("search", [])]


def _search_fulltext(query):
    data = _get(WIKIDATA_API, {"action": "query", "list": "search", "srsearch": query, "srlimit": 5})
    # la búsqueda a texto completo no da etiqueta: el fragmento hace las veces
    return [{"id": h["title"], "label": re.sub(r"<[^>]+>", "", h.get("snippet", "") or ""),
             "description": re.sub(r"<[^>]+>", "", h.get("snippet", "") or "")}
            for h in data.get("query", {}).get("search", []) if h.get("title", "").startswith("Q")]


def _fold(text):
    """minúsculas y sin tildes, para comparar nombres"""
    text = unicodedata.normalize("NFD", str(text or ""))
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


# "ciudad de X" no es el edificio aunque caiga al lado. Las descripciones de Wikidata empiezan
# por el tipo ("neighborhood in Kyoto"), así que se ancla al principio: "building in Porto,
# Porto District, Portugal" no es un barrio, solo lleva el distrito en la dirección.
ADMIN_AREA = re.compile(r"^\W*(former\s+|the\s+|antigu[oa]\s+)*"
                        r"(city|town|village|municipality|ward|prefecture|district|"
                        r"neighbo(u)?rhood|county|region|island|mountain|river|"
                        r"ciudad|pueblo|aldea|municipio|barrio|distrito|prefectura|regi[óo]n|isla|"
                        r"monta[ñn]a|monte|r[íi]o|localidad)\b\s*(in|of|,|de|del|en|$)", re.I)


def _words(text):
    """palabras significativas, sin lo que va entre paréntesis"""
    text = re.sub(r"\s*\([^)]*\)", " ", str(text or ""))
    return [t for t in re.findall(r"[a-z0-9]+", _fold(text)) if len(t) >= 3]


# "Higashi Chaya District" y "Higashichaya" son el mismo barrio; "Musashino" y "Musashino Place", no
PLACE_CATEGORY = {"district", "distrito", "barrio", "quarter", "neighborhood", "neighbourhood", "area", "zona"}


def _same_place(label, name):
    """Mismo sitio escrito de otra forma: junto o separado, con o sin la coletilla del tipo."""
    glue = lambda words: "".join(w for w in words if w not in PLACE_CATEGORY)
    return bool(glue(_words(name))) and glue(_words(name)) == glue(_words(label))


def _label_covers(label, name):
    """¿La etiqueta de Wikidata dice lo mismo que el nombre del hito?

    El nombre tiene que aparecer entero y seguido dentro de la etiqueta (o al revés, pegado todo,
    que es como se transcribe del japonés: "Higashichaya" ↔ "Higashi Chaya"); si además el nombre
    es de una sola palabra, la etiqueta no puede añadir nada distintivo. Sin este control "Garden
    & House" acaba en la revista, "House NA" en el artículo "casa" y TIME'S en el análisis de
    series temporales; con él, "Musashino Place Stadtbibliothek" sigue valiendo."""
    keys, lab = _words(name), _words(label)
    if not keys or not lab:
        return False
    run = any(lab[i:i + len(keys)] == keys for i in range(len(lab) - len(keys) + 1))
    glued = "".join(keys) in "".join(lab) or "".join(lab) in "".join(keys)
    if not (run or glued):
        return False
    return len(keys) >= 2 or not (set(lab) - set(keys) - GENERIC_WORDS)


def _pick(hits, lat, lon, strict, name="", allow_area=False):
    """Elige la ficha de Wikidata que de verdad puede ser este hito.

    Vale si cae cerca y, o bien se llama igual, o al menos está descrita como un lugar; si nada
    tiene coordenadas que lo corroboren, solo vale que se llame igual. Sin esa segunda regla la
    búsqueda se queda con lo primero que tenga foto: la revista House & Garden, el artículo
    "casa" o el emperador Go-Kōmyō."""
    hits = [h for h in hits if not BAD_HIT.search(h["description"])
            and not (PERSON_WORDS.search(h["description"]) and not BUILDING_WORDS.search(h["description"]))][:4]
    if not hits:
        return None
    ents = _get(WIKIDATA_API, {"action": "wbgetentities", "ids": "|".join(h["id"] for h in hits),
                               "props": "claims|sitelinks", "sitefilter": "enwiki|eswiki|jawiki"}).get("entities", {})
    infos = [(h, _entity_info(h["id"], ents.get(h["id"], {}))) for h in hits]
    near = lambda info: info["lat"] is not None and lat is not None and haversine_km(lat, lon, info["lat"], info["lon"]) <= NEAR_KM
    placey = lambda h: bool(BUILDING_WORDS.search(h["description"]))
    if lat is not None:   # an entity that sits far from where the landmark is cannot be it (namesakes abroad)
        infos = [(h, info) for h, info in infos if info["lat"] is None or near(info)]
    if strict:   # último recurso: cerca, descrita como lugar y sin que sea el barrio o la ciudad
        for h, info in infos:
            if near(info) and placey(h) and not ADMIN_AREA.search(h["description"]):
                return info
        return None
    for h, info in infos:      # 1) cerca del hito y, o se llama igual, o suena a lugar
        if not near(info):
            continue
        if ADMIN_AREA.search(h["description"]) and not allow_area:
            # el barrio o la ciudad donde está un edificio no son el edificio; solo valen cuando
            # el hito es el propio barrio (mismo nombre, o el profesor lo llamó "… (barrio)").
            # Para una parada de la ruta sí buscamos la ciudad: allow_area.
            if _same_place(h.get("label"), name) or set(_words(name)) & PLACE_CATEGORY:
                return info
            continue
        return info
    # sin coordenadas que confirmen nada, la etiqueta tiene que traer todas las palabras del nombre
    named = [(h, info) for h, info in infos if _label_covers(h.get("label"), name)]
    for h, info in named:      # 2) además, descrita como un lugar
        if placey(h):
            return info
    for h, info in named:      # 3) o al menos con fotos y sin coordenadas que la contradigan
        if info["lat"] is None and (info["image"] or info["category"]):
            return info
    return None


def wikidata_lookup(name, lat=None, lon=None, allow_area=False):
    """-> entity info dict or None. Label search (English, then Spanish), then full-text
    search, then the distinctive part of the name with strict checks."""
    plain = re.sub(r"\s*\([^)]*\)", "", name).strip() or name   # "Gion (barrio)" -> "Gion"
    for lang in ("en", "es"):   # professors write names in Spanish or English
        info = _pick(_search_labels(plain, lang), lat, lon, strict=False, name=name, allow_area=allow_area)
        if info:
            return info
    info = _pick(_search_fulltext(plain), lat, lon, strict=False, name=name, allow_area=allow_area)
    if info:
        return info
    core = core_name(name)
    if core:
        return _pick(_search_labels(core, "en"), lat, lon, strict=True, name=core)
    return None


def _files_from_pages(pages):
    out = []
    for p in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        ii = (p.get("imageinfo") or [{}])[0]
        mime = ii.get("mime", "")
        if mime not in PICTURE_MIMES or "thumburl" not in ii:   # no djvu/pdf/tiff scans, no video
            continue
        title = p["title"].split(":", 1)[-1]
        coords = (p.get("coordinates") or [{}])[0]
        out.append({
            "title": title,
            "mime": mime,
            "thumb": ii["thumburl"],
            "url": file_url(title, LARGE_WIDTH),   # a resized copy, never the multi-MB original
            "page_url": ii.get("descriptionurl") or "https://commons.wikimedia.org/wiki/" + quote(p["title"].replace(" ", "_")),
            "lat": coords.get("lat"), "lon": coords.get("lon"),
        })
    return out


def commons_category_files(category, limit=12):
    data = _get(COMMONS_API, {"action": "query", "generator": "categorymembers", "gcmtitle": "Category:" + category,
                              "gcmtype": "file", "gcmlimit": limit, "prop": "imageinfo",
                              "iiprop": "url|mime", "iiurlwidth": THUMB_WIDTH})
    return _files_from_pages(data.get("query", {}).get("pages", {}))


def commons_search(query, limit=10):
    data = _get(COMMONS_API, {"action": "query", "generator": "search", "gsrsearch": query, "gsrnamespace": 6,
                              "gsrlimit": limit, "prop": "imageinfo|coordinates", "iiprop": "url|mime",
                              "iiurlwidth": THUMB_WIDTH})
    return _files_from_pages(data.get("query", {}).get("pages", {}))


def commons_drawings(category, limit=8):
    q = (f'incategory:"{category}" (plan OR section OR elevation OR drawing OR sketch OR diagram OR '
         f'Grundriss OR Schnitt OR planta OR sección OR alzado OR 平面図 OR 断面図 OR 立面図 OR 図面)')
    return commons_search(q, limit)


DRAWING_MIMES = {"image/png", "image/svg+xml", "image/gif"}
PICTURE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml"}


def is_drawing(title, mime=None):
    """Drawings are named as such, or are line art (png/svg) rather than photos (jpeg)."""
    return bool(DRAWING_WORDS.search(title)) or (mime in DRAWING_MIMES and not re.search(r"photo|foto", title, re.I))


def city_photo(city, country=None, lat=None, lon=None):
    """Una foto de la ciudad para la tira de la ruta: la imagen principal de su ficha de
    Wikidata y, si no la tiene, la primera de su categoría en Commons. None si no hay nada."""
    wd = wikidata_lookup(city, lat, lon, allow_area=True)
    if not wd:
        return None
    best = wd["image"]
    if not best and wd["category"]:
        files = [f for f in commons_category_files(wd["category"]) if not is_drawing(f["title"], f.get("mime"))]
        if files:
            f = files[0]
            return {"url": f["url"], "thumb": f["thumb"], "title": f["title"], "page_url": f["page_url"],
                    "wikidata_id": wd["id"], "wikipedia_url": wd["wikipedia"]}
    if not best:
        return None
    return {"url": file_url(best, LARGE_WIDTH), "thumb": file_url(best, THUMB_WIDTH), "title": best,
            "page_url": "https://commons.wikimedia.org/wiki/File:" + quote(best.replace(" ", "_")),
            "wikidata_id": wd["id"], "wikipedia_url": wd["wikipedia"]}


def _same_keywords(a, b):
    """Mismo edificio dicho en dos idiomas: "Museo de Arte de Miyagi" / "Miyagi Museum of Art".
    Se comparan solo las palabras con chicha, sin museo/arte/casa/of/de… Una sola palabra corta
    no identifica nada: "TIME'S" coincidiría con "Time's Up"."""
    ka = set(_words(a)) - GENERIC_WORDS
    kb = set(_words(b)) - GENERIC_WORDS
    if not ka or ka != kb:
        return False
    return len(ka) >= 2 or len(next(iter(ka))) >= 5


def _distinctive(name):
    return {w for w in _words(name) if w not in GENERIC_WORDS}


def wikipedia_lookup(name, lat=None, lon=None):
    """La ficha del hito en Wikipedia: su foto de cabecera y, de paso, su id de Wikidata.

    Es la red de seguridad cuando la búsqueda en Wikidata no encuentra nada, que es lo que pasa
    con los nombres escritos en español ("Santuario Ōsaki Hachimangū"): Wikipedia busca por texto
    y sí los reconoce. Solo vale si el artículo cae cerca del hito o se titula como él."""
    keys = _distinctive(name)
    if not keys or (len(keys) < 2 and max(len(w) for w in keys) < 5):
        return None                  # "TIME'S", "House NA": con eso no se busca a ciegas
    plain = re.sub(r"\s*\([^)]*\)", "", name).strip() or name
    for lang in WIKI_LANGS:
        data = _get(WIKIPEDIA_API % lang, {
            "action": "query", "generator": "search", "gsrsearch": plain, "gsrlimit": 5,
            "prop": "pageimages|coordinates|pageprops|description", "piprop": "thumbnail|name",
            "pithumbsize": THUMB_WIDTH, "ppprop": "wikibase_item"})
        pages = sorted((data.get("query", {}) or {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
        for page in pages:
            if not page.get("pageimage"):
                continue
            coords = (page.get("coordinates") or [{}])[0]
            if (lat is not None and coords.get("lat") is not None
                    and haversine_km(lat, lon, coords["lat"], coords["lon"]) > WIKI_KM):
                continue        # otro sitio con el mismo nombre: el Kōmyō-in de Sakai, no el de Kioto
            # sin esto, "Museo de Arte de Miyagi" se queda con el templo Zuigan-ji, que está al lado
            if not (_label_covers(page["title"], name) or _same_keywords(page["title"], name)):
                continue
            extra = _distinctive(name) - _distinctive(page["title"])
            if extra and extra <= GENERIC_WORDS | {"house", "casa"}:
                continue        # "Shibaura" no es "Shibaura House": es el barrio donde está
            desc = page.get("description") or ""
            if BAD_HIT.search(desc) or (PERSON_WORDS.search(desc) and not BUILDING_WORDS.search(desc)):
                continue        # una persona, un disco, una revista…
            if ADMIN_AREA.search(desc) and not (_same_place(page["title"], name)
                                                or _distinctive(name) & PLACE_CATEGORY):
                continue        # el barrio donde está el edificio no es el edificio
            return {"id": (page.get("pageprops") or {}).get("wikibase_item"),
                    "image": page["pageimage"],
                    "title": page["title"],
                    "wikipedia": "https://%s.wikipedia.org/wiki/%s" % (lang, quote(page["title"].replace(" ", "_"))),
                    "lat": coords.get("lat"), "lon": coords.get("lon")}
    return None


GOOGLE_CSE = "https://www.googleapis.com/customsearch/v1"
OPENVERSE = "https://api.openverse.org/v1/images/"


def google_photo(name, architect="", site=""):
    """-> la primera foto de Google Imágenes, o None si no hay clave configurada.

    La página de resultados de Google no se puede leer desde un servidor (responde 302 a un
    muro de consentimiento), pero su API de búsqueda sí, con una clave gratuita de 100
    consultas al día: `ARCHTRIP_GOOGLE_KEY` + `ARCHTRIP_GOOGLE_CX` (un buscador programable
    con «buscar en toda la web» activado). Sin esas variables, esto no existe y se pasa a las
    otras fuentes."""
    key, cx = os.environ.get("ARCHTRIP_GOOGLE_KEY"), os.environ.get("ARCHTRIP_GOOGLE_CX")
    query = " ".join(x for x in (name, architect, site) if x).strip()
    if not (key and cx and query):
        return None
    _throttle()
    resp = requests.get(GOOGLE_CSE, params={"key": key, "cx": cx, "q": query, "searchType": "image",
                                            "num": 5, "safe": "active", "imgSize": "large"},
                        headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    if resp.status_code != 200:
        return None
    for hit in resp.json().get("items") or []:
        img = hit.get("image") or {}
        if (img.get("width") or 0) < 500:          # miniaturas y logotipos, fuera
            continue
        return {"url": hit.get("link"), "thumb": img.get("thumbnailLink") or hit.get("link"),
                "title": hit.get("title") or name, "page": img.get("contextLink"), "source": "google"}
    return None


def openverse_photo(name, architect=""):
    """-> {url, thumb, title, page, source} de la primera foto con licencia libre que de verdad
    sea de esta obra, o None. Openverse busca en Flickr, Commons y demás, sin clave; de ahí salen
    muchas obras contemporáneas que Wikidata no tiene fichadas."""
    query = " ".join(x for x in (name, architect) if x).strip()
    if not query:
        return None
    _throttle()
    resp = requests.get(OPENVERSE, params={"q": query, "page_size": 10, "mature": "false"},
                        headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    if resp.status_code != 200:
        return None
    for hit in resp.json().get("results") or []:
        title = hit.get("title") or ""
        url = hit.get("url")
        # el buscador devuelve lo que se le parece: solo vale si el título es el del edificio
        if not url or not (_same_keywords(title, name) or _distinctive(name) <= set(_words(title))):
            continue
        creator = hit.get("creator") or ""
        return {"url": url, "thumb": hit.get("thumbnail") or url, "source": "openverse",
                "title": title + (" · " + creator if creator else ""),
                "page": hit.get("foreign_landing_url") or hit.get("url")}
    return None


def entity_info(qid):
    """Los datos de una ficha de Wikidata que ya conocemos por su id."""
    ents = _get(WIKIDATA_API, {"action": "wbgetentities", "ids": qid, "props": "claims|sitelinks",
                               "sitefilter": "enwiki|eswiki|jawiki"}).get("entities", {})
    return _entity_info(qid, ents.get(qid, {}))


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
        # Wikidata no lo conoce por ese nombre: a ver si Wikipedia sí
        wp = wikipedia_lookup(name, lat, lon)
        if wp:
            result["wikipedia_url"] = result["wikipedia_url"] or wp["wikipedia"]
            result["wikidata_id"] = result["wikidata_id"] or wp["id"]
            if result["lat"] is None:
                result["lat"], result["lon"] = wp["lat"], wp["lon"]
            add({"title": wp["image"], "url": file_url(wp["image"], LARGE_WIDTH),
                 "thumb": file_url(wp["image"], THUMB_WIDTH),
                 "page_url": "https://commons.wikimedia.org/wiki/File:" + quote(wp["image"].replace(" ", "_"))},
                "foto", "wikipedia")
            if wp["id"]:          # con su ficha de Wikidata a mano, su categoría trae más fotos
                try:
                    info = entity_info(wp["id"])
                    for pl in info["plans"]:
                        add({"title": pl, "url": file_url(pl, LARGE_WIDTH), "thumb": file_url(pl, THUMB_WIDTH),
                             "page_url": "https://commons.wikimedia.org/wiki/File:" + quote(pl.replace(" ", "_"))},
                            "plano", "wikidata")
                    if info["category"]:
                        for f in commons_category_files(info["category"]):
                            add(f, "plano" if is_drawing(f["title"], f.get("mime")) else "foto", "commons")
                except Exception:
                    pass
    result["images"] = [{k: v for k, v in im.items() if k != "mime"} for im in photos[:MAX_PHOTOS] + drawings[:MAX_DRAWINGS]]
    return result
