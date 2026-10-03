"""Autocompletado: qué más hay cerca de lo que ya tienes.

La geometría, en dos piezas:

1. **La envolvente** — un disco alrededor de cada parada y de cada hito ya curado: hasta dónde se
   puede llegar en coche en N horas (2 por defecto). Es el radio real de acción del viaje.
2. **El corredor** — una banda a lo largo de la carretera que une las paradas, con el desvío que
   se admite sin romper el día (1 hora por defecto). Lo que cae "de camino".

Las distancias se convierten a tiempo con los mismos números que `geo.estimate_drive`
(línea recta ×1,3 a 70 km/h), así que una hora son ~54 km y dos ~108. Es una aproximación
deliberada: calcular isócronas de verdad pediría un servicio de rutas de pago.

Y tres fuentes, de la más curada a la más amplia:

- **Arquitectura Viva** (`arquitecturaviva`) — su mapa de obras se sirve como un único JSON
  estático (`/assets/uploads/obras/all-es.json`, ~4.400 obras, todas con coordenadas), con
  título y arquitecto en español y enlace directo a la ficha. Es una selección editorial: lo
  que una revista de arquitectura ha decidido publicar.
- **Iwan Baan** (`iwanbaan`) — su portfolio (WordPress, 675 proyectos) es el canon de la
  arquitectura contemporánea fotografiada, pero **no publica coordenadas**: solo lugar
  (país/ciudad) y arquitecto, así que se sitúa geocodificando el nombre del lugar, con un tope
  de llamadas por consulta para no colgarse (el resto aparece sin situar y se afina al repetir).
- **Wikidata** (`wikidata`) — todo lo que tiene arquitecto declarado (P84). Enciclopédico, no
  curado: trae de todo (estaciones, naves, iglesias de pueblo) pero también lo que falta en las
  otras dos, con coordenadas exactas y foto libre en Commons.

De cada fuente salen *candidatos*, nunca hitos: se revisan y se importan a mano. Las fotos solo
se importan cuando son libres (Commons); de AV y de Iwan Baan se enseña la miniatura para
reconocer el edificio, pero la foto del hito la busca después el enriquecido donde siempre."""
import hashlib
import html
import json
import math
import re
import time
from pathlib import Path
from urllib.parse import quote, unquote

import requests

from .geo import haversine_km
from .images import file_url

USER_AGENT = "archTrip/0.1 (https://github.com/josevzs/archTrip; herramienta docente)"
KMH, DETOUR = 70.0, 1.3          # los mismos de geo.estimate_drive
ENVELOPE_H, HALO_H = 2.0, 1.0    # horas por defecto
MAX_HOURS = 6                    # más allá de esto no es un viaje, es otra cosa
CACHE_DAYS = 7
OSRM_ROUTE = "https://router.project-osrm.org/route/v1/driving/"

SOURCES = ("arquitecturaviva", "iwanbaan", "wikidata")
CURATED = ("arquitecturaviva", "iwanbaan")   # las listas escogidas a mano, que nunca se recortan
SOURCE_NAME = {"arquitecturaviva": "Arquitectura Viva", "iwanbaan": "Iwan Baan", "wikidata": "Wikidata"}


# --------------------------------------------------------------------- geometría

def radius_km(hours):
    """Horas en coche -> kilómetros en línea recta, con el mismo criterio que el resto de la app."""
    return max(0.0, float(hours)) * KMH / DETOUR


def drive_minutes(km):
    return km * DETOUR / KMH * 60


def centres(stops, landmarks, include_posible=False):
    """Desde dónde se mide la envolvente: las paradas y los hitos que ya están decididos."""
    keep = {"curado", "posible"} if include_posible else {"curado"}
    pts = [(s["lat"], s["lon"]) for s in stops if s.get("lat") is not None]
    pts += [(lm["lat"], lm["lon"]) for lm in landmarks
            if lm.get("lat") is not None and lm.get("status") in keep]
    return pts


def road_line(stops, timeout=20):
    """La carretera que une las paradas, en orden. Si el servicio de rutas no contesta, la recta
    entre paradas, que para medir desvíos da casi lo mismo."""
    located = [s for s in stops if s.get("lat") is not None]
    straight = [(s["lat"], s["lon"]) for s in located]
    if len(located) < 2:
        return straight
    coords = ";".join(f"{s['lon']},{s['lat']}" for s in located)
    try:
        resp = requests.get(OSRM_ROUTE + coords, params={"overview": "simplified", "geometries": "geojson"},
                            headers={"User-Agent": USER_AGENT}, timeout=timeout)
        geom = resp.json()["routes"][0]["geometry"]["coordinates"]
        return [(lat, lon) for lon, lat in geom]
    except Exception:
        return straight


def _segment_km(lat, lon, a, b):
    """Distancia de un punto al segmento a-b, en plano local (sobra para decenas de km)."""
    scale = math.cos(math.radians(lat))
    ax, ay = (a[1] - lon) * scale, a[0] - lat
    bx, by = (b[1] - lon) * scale, b[0] - lat
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        t = 0.0
    else:
        t = max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    px, py = ax + t * dx, ay + t * dy
    return math.hypot(px, py) * 111.32


def line_km(lat, lon, line):
    """Distancia a la polilínea de la carretera."""
    if not line:
        return None
    if len(line) == 1:
        return haversine_km(lat, lon, line[0][0], line[0][1])
    return min(_segment_km(lat, lon, line[i], line[i + 1]) for i in range(len(line) - 1))


def nearest_km(lat, lon, points):
    return min((haversine_km(lat, lon, p[0], p[1]) for p in points), default=None)


def bbox(points, line, margin_km):
    """El rectángulo que se le pide a cada fuente: todo lo anterior, ensanchado."""
    pts = list(points) + list(line)
    if not pts:
        return None
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    dlat = margin_km / 111.32
    mid = math.cos(math.radians(sum(lats) / len(lats))) or 1
    dlon = margin_km / (111.32 * max(0.2, mid))
    return (min(lats) - dlat, min(lons) - dlon, max(lats) + dlat, max(lons) + dlon)


def in_box(lat, lon, box):
    return box is None or (box[0] <= lat <= box[2] and box[1] <= lon <= box[3])


# ------------------------------------------------------------------------ caché

def _cache_file(cache_dir, name):
    return Path(cache_dir) / name if cache_dir else None


def cached_json(cache_dir, name, build, days=CACHE_DAYS):
    """Las tres fuentes son lentas o grandes y cambian poco: se guardan en disco una semana."""
    path = _cache_file(cache_dir, name)
    if path and path.exists() and time.time() - path.stat().st_mtime < days * 86400:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    data = build()
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _get(url, params=None, timeout=60, accept=None):
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    resp = requests.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp


# ------------------------------------------------------- fuente: Arquitectura Viva

AV_SITE = "https://arquitecturaviva.com"
AV_JSON = AV_SITE + "/assets/uploads/obras/all-es.json"


def av_works(cache_dir=None, timeout=90):
    """El mapa de obras de Arquitectura Viva tal cual lo sirve su web (un JSON de ~1,4 MB)."""
    return cached_json(cache_dir, "arquitecturaviva.json",
                       lambda: _get(AV_JSON, timeout=timeout).json())


def av_candidates(box, cache_dir=None):
    out = []
    for w in av_works(cache_dir):
        try:
            lat, lon = (float(v) for v in (w.get("coords") or "").split(","))
        except (ValueError, TypeError):
            continue
        if not in_box(lat, lon, box):
            continue
        slug = w.get("slug") or ""
        thumb = None
        if w.get("img"):
            thumb = f"{AV_SITE}/assets/uploads/obras/{w['id']}/av_thumb__{w['img']}"
            if w.get("hash"):
                thumb += "?h=" + w["hash"]
        out.append({
            "source": "arquitecturaviva", "ref": str(w.get("id")), "name": w.get("title") or slug,
            "architects": list(w.get("author") or []), "lat": lat, "lon": lon,
            "city": (w.get("city") or [None])[0], "country": (w.get("country") or [None])[0],
            "year": " ".join(str(w.get("date") or "").split()) or None, "precision": "exacta",
            "url": f"{AV_SITE}/obras/{slug}" if slug else AV_SITE + "/mapa",
            "thumb": thumb,            # solo para reconocer el edificio en la lista
            "url_av": f"{AV_SITE}/obras/{slug}" if slug else None,
        })
    return out


# ------------------------------------------------------------- fuente: Iwan Baan

IWAN_API = "https://iwan.com/wp-json/wp/v2/"
IWAN_GEOCODE_BUDGET = 8      # lugares nuevos que se sitúan en cada consulta


def _wp_all(path, fields, timeout=60):
    out, page = [], 1
    while page <= 20:
        resp = _get(IWAN_API + path, {"per_page": 100, "page": page, "_fields": fields}, timeout)
        chunk = resp.json()
        out += chunk
        if len(chunk) < 100:
            break
        page += 1
    return out


def iwan_index(cache_dir=None, timeout=60):
    """Proyectos, lugares y arquitectos del portfolio, en bruto."""
    def build():
        return {"places": _wp_all("places", "id,name,parent,count", timeout),
                "architects": _wp_all("architects", "id,name", timeout),
                "projects": _wp_all("jetpack-portfolio", "id,slug,link,title,architects,places,date", timeout)}
    return cached_json(cache_dir, "iwanbaan.json", build)


def _clean(text):
    """Los títulos vienen con entidades HTML y con el arquitecto pegado detrás de un guion."""
    text = html.unescape(re.sub(r"<[^>]+>", "", str(text or "")))
    return re.sub(r"\s+", " ", text).strip()


def _split_title(title):
    """«House NA – Sou Fujimoto» -> («House NA», «Sou Fujimoto»)"""
    parts = re.split(r"\s+[–—-]\s+", _clean(title))
    return (parts[0].strip(), parts[-1].strip()) if len(parts) > 1 else (_clean(title), "")


COARSE_KM = 120        # más ancho que esto es una región o un país, no sitúa un edificio
PROJECT_KEY = "obra:"  # en la caché de lugares, lo geocodificado a partir del título de una obra
EXACT = ("exacta", "geocodificado")   # precisiones que sí sitúan el edificio


def _is_coarse(place):
    return place.get("extent_km") is None or place["extent_km"] > COARSE_KM


def _place_counts(index, names):
    counts = {}
    for p in index["projects"]:
        for t in p.get("places") or []:
            if t in names:
                counts[names[t]] = counts.get(names[t], 0) + 1
    return counts


def _priority(index, names, located, box, hints):
    """En qué orden conviene gastar las geocodificaciones: primero los lugares que suenan a algo
    del viaje o que acompañan a un lugar ya situado dentro del rectángulo (si «Japan» ya está
    dentro, las ciudades japonesas pasan al frente), y después los que más proyectos tienen."""
    inside = {n for n, v in located.items() if v and in_box(v["lat"], v["lon"], box)}
    friends = set()
    for p in index["projects"]:
        terms = [names[t] for t in (p.get("places") or []) if t in names]
        if any(t in inside for t in terms):
            friends.update(terms)
    counts = _place_counts(index, names)
    pend = [n for n in counts if n not in located]
    pend.sort(key=lambda n: (0 if (n in friends or n.lower() in hints) else 1, -counts[n], n))
    return pend


def _coarse_only(index, names, located, box):
    """Proyectos que caen en la zona solo por su país: su sitio hay que sacarlo del título
    («Sendai Mediatheque», «Tsuruoka Cultural Center»), que es lo único que dice dónde están."""
    out = []
    for p in index["projects"]:
        terms = [names[t] for t in (p.get("places") or []) if t in names]
        inside = [located[t] for t in terms if located.get(t) and in_box(located[t]["lat"], located[t]["lon"], box)]
        if inside and all(_is_coarse(v) for v in inside):
            name, _ = _split_title(p.get("title", {}).get("rendered"))
            out.append((p["slug"], name + ", " + terms[-1] if terms else name))
    return out


def iwan_places(cache_dir=None, geocode=None, box=None, budget=IWAN_GEOCODE_BUDGET, hints=()):
    """Sitúa el portfolio geocodificando nombres, de pocos en pocos y guardando el resultado para
    siempre: Iwan Baan no publica coordenadas y no vamos a tener a nadie esperando un minuto.
    Primero los lugares (cada uno sitúa varios proyectos de golpe) y después, uno a uno, los
    proyectos que solo saben decir el país. -> (lo que se sabe, cuánto queda por situar)."""
    path = _cache_file(cache_dir, "iwanbaan-lugares.json")
    located = {}
    if path and path.exists():
        try:
            located = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            located = {}
    if geocode is None or box is None:
        return located, 0
    index = iwan_index(cache_dir)
    names = {p["id"]: _clean(p["name"]) for p in index["places"]}
    pend = [(n, n) for n in _priority(index, names, located, box, {h.lower() for h in hints})]
    done = 0
    for key, query in pend:
        if done >= budget:
            break
        try:
            located[key] = geocode(query)
        except Exception:
            break                      # sin red: lo que haya y a seguir
        done += 1
    left = max(0, len(pend) - done)
    if not left:                       # con los lugares resueltos, los proyectos sueltos
        projects = [(PROJECT_KEY + s, q) for s, q in _coarse_only(index, names, located, box)
                    if PROJECT_KEY + s not in located]
        for key, query in projects:
            if done >= budget:
                break
            try:
                located[key] = geocode(query)
            except Exception:
                break
            done += 1
        left = max(0, len(projects) - max(0, done - len(pend)))
    if path and done:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(located, ensure_ascii=False), encoding="utf-8")
    return located, left


def iwan_candidates(box, cache_dir=None, geocode=None, budget=IWAN_GEOCODE_BUDGET, hints=()):
    """-> (candidatos, lugares que quedan por situar). Un proyecto situado solo por su país sale
    igualmente, marcado `precision='pais'`: no se puede medir su desvío, pero en un viaje a Japón
    merece la pena verlo (el enriquecido lo localizará al importarlo)."""
    index = iwan_index(cache_dir)
    names = {p["id"]: _clean(p["name"]) for p in index["places"]}
    archs = {a["id"]: _clean(a["name"]) for a in index["architects"]}
    located, remaining = iwan_places(cache_dir, geocode, box, budget, hints)
    out = []
    for p in index["projects"]:
        places = [names[t] for t in (p.get("places") or []) if t in names]
        inside = [(n, located[n]) for n in places
                  if located.get(n) and in_box(located[n]["lat"], located[n]["lon"], box)]
        fine = [(n, v) for n, v in inside if not _is_coarse(v)]
        if not inside:
            continue
        # el título del proyecto, si se ha podido geocodificar, manda sobre la ciudad y el país
        own = located.get(PROJECT_KEY + (p.get("slug") or ""))
        if own and not _is_coarse(own):
            if not in_box(own["lat"], own["lon"], box):
                continue
            fine = [(None, own)]       # las coordenadas son de la obra; la ciudad, la que diga el hito
        place, point = (fine or inside)[0]
        name, tail = _split_title(p.get("title", {}).get("rendered"))
        authors = [archs[a] for a in (p.get("architects") or []) if a in archs] or ([tail] if tail else [])
        out.append({
            "source": "iwanbaan", "ref": p.get("slug"), "name": name,
            "architects": _dedupe_authors(authors), "lat": point["lat"], "lon": point["lon"],
            "city": place if fine else None, "country": places[-1] if places else None,
            "year": (p.get("date") or "")[:4] or None,
            "precision": ("geocodificado" if own and not _is_coarse(own) else "lugar") if fine else "pais",
            "url": p.get("link"), "thumb": None,     # sus fotos son suyas: solo enlazamos
            "place_names": places,
        })
    return out, remaining


STUDIO_WORDS = {"associates", "associati", "architects", "architecture", "architekten", "arquitectos",
                "arquitectura", "office", "partners", "studio", "atelier", "and", "y", "&", "+"}


def _person(name):
    """El nombre sin la parte de «estudio», para ver si dos entradas son el mismo autor."""
    words = re.findall(r"\w+", name.lower())
    return {w for w in words if w not in STUDIO_WORDS}


def _dedupe_authors(names):
    """El portfolio repite al arquitecto de varias formas («Junya Ishigami», «Junya Ishigami +
    Associates», «Eduardo Souto de Moura» y «Souto de Moura Arquitectos»): uno basta."""
    out = []
    for n in names:
        if not n:
            continue
        words = _person(n)
        if any(words <= _person(o) or _person(o) <= words for o in out):
            continue
        out.append(n)
    return out


# --------------------------------------------------------------- fuente: Wikidata

SPARQL = "https://query.wikidata.org/sparql"
QUERY = """
SELECT ?item ?itemLabel ?coord ?placeLabel ?inception ?image ?architectLabel WHERE {
  SERVICE wikibase:box {
    ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:cornerSouthWest "Point(%(west)f %(south)f)"^^geo:wktLiteral .
    bd:serviceParam wikibase:cornerNorthEast "Point(%(east)f %(north)f)"^^geo:wktLiteral .
  }
  ?item wdt:P84 ?architect .
  OPTIONAL { ?item wdt:P131 ?place }
  OPTIONAL { ?item wdt:P571 ?inception }
  OPTIONAL { ?item wdt:P18 ?image }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "es,en,ja". }
}
"""


def wikidata_architecture(box, cache_dir=None, timeout=120):
    """Todo lo que Wikidata sabe que es arquitectura dentro del rectángulo. La consulta tarda
    decenas de segundos para un país entero, así que también se guarda en disco."""
    south, west, north, east = box

    def build():
        resp = _get(SPARQL, {"query": QUERY % {"south": south, "west": west, "north": north, "east": east}},
                    timeout, accept="application/sparql-results+json")
        items = {}
        for r in resp.json()["results"]["bindings"]:
            qid = r["item"]["value"].rsplit("/", 1)[-1]
            point = r["coord"]["value"]                      # Point(lon lat)
            try:
                lon, lat = (float(v) for v in point[point.index("(") + 1:point.index(")")].split())
            except Exception:
                continue
            item = items.setdefault(qid, {
                "source": "wikidata", "ref": qid, "name": r["itemLabel"]["value"], "lat": lat, "lon": lon,
                "city": r.get("placeLabel", {}).get("value"), "country": None,
                "year": (r.get("inception", {}).get("value") or "")[:4] or None,
                "precision": "exacta", "url": "https://www.wikidata.org/wiki/" + qid,
                "architects": [], **commons_photo(r.get("image", {}).get("value")),
            })
            arch = r.get("architectLabel", {}).get("value")
            if arch and arch not in item["architects"] and not re.fullmatch(r"Q\d+", arch):
                item["architects"].append(arch)
        return list(items.values())

    key = hashlib.sha1(("%.3f|%.3f|%.3f|%.3f" % box).encode()).hexdigest()[:12]
    return cached_json(cache_dir, f"wikidata-{key}.json", build)


def commons_photo(image):
    """La foto que Wikidata ya trae (P18): grande, miniatura y página del archivo. Es de Commons,
    o sea libre, así que esta sí se puede importar con el hito."""
    if not image:
        return {}
    name = unquote(image.rsplit("/", 1)[-1])
    return {"photo": file_url(name, 1600), "thumb": file_url(name, 640), "photo_title": name,
            "photo_page": "https://commons.wikimedia.org/wiki/File:" + quote(name.replace(" ", "_"))}


# ------------------------------------------------------------------- lo que ya hay

SAME_NAME_KM = 50      # dos edificios con el mismo nombre tan cerca son el mismo
SAME_SPOT_KM = 0.08    # en el mismo punto (80 m) es el mismo edificio aunque lo llamen distinto
NEAR_SPOT_KM = 0.25    # a 250 m hace falta algo más: un barrio denso tiene mil cosas a 250 m


def _name_words(fold, text):
    return {w for w in re.findall(r"\w{4,}", fold(text))}


def same_building(fold, name_a, arch_a, lat_a, lon_a, name_b, arch_b, lat_b, lon_b):
    """¿Son el mismo edificio dos fichas de dos sitios distintos? Las coordenadas de una misma
    obra bailan cien metros entre fuentes, pero en Shinjuku o en una Expo hay obras distintas a
    esa misma distancia, así que a partir de 80 m hace falta que coincida además el nombre o el
    arquitecto. Y un nombre igual a pocos kilómetros vale por sí solo."""
    if lat_a is None or lat_b is None:
        return fold(name_a) == fold(name_b)
    km = haversine_km(lat_a, lon_a, lat_b, lon_b)
    fa, fb = fold(name_a), fold(name_b)
    if fa and fa == fb:
        return km <= SAME_NAME_KM
    if km > NEAR_SPOT_KM:
        return False
    if km <= SAME_SPOT_KM:
        return True
    if fa and fb and (fa in fb or fb in fa):
        return True
    return bool(_name_words(fold, arch_a) & _name_words(fold, arch_b))


def known(landmarks):
    """Lo que ya está en el viaje, para no proponerlo otra vez."""
    from .excel import landmark_key, normalise
    return {
        "qids": {lm["wikidata_id"] for lm in landmarks if lm.get("wikidata_id")},
        "keys": {lm["name_key"] for lm in landmarks if lm.get("name_key")},
        "mine": [(lm.get("name") or "", lm.get("architect") or "", lm.get("lat"), lm.get("lon"))
                 for lm in landmarks],
        "key_of": landmark_key, "fold": normalise,
    }


def is_known(cand, seen):
    """Wikidata suele listar varios arquitectos (el estudio y la persona) y cada fuente escribe
    los nombres a su manera, así que la identidad del Excel (nombre|arquitecto) se queda corta."""
    if cand.get("source") == "wikidata" and cand["ref"] in seen["qids"]:
        return True
    if seen["key_of"](cand["name"], ", ".join(cand["architects"])) in seen["keys"]:
        return True
    lat, lon = (cand["lat"], cand["lon"]) if cand.get("precision") in EXACT else (None, None)
    arch = ", ".join(cand["architects"])
    return any(same_building(seen["fold"], cand["name"], arch, lat, lon, name, architect, mlat, mlon)
               for name, architect, mlat, mlon in seen["mine"])


def merge_sources(cands, fold):
    """Un mismo edificio en dos fuentes es un solo candidato que las cita a las dos (y así se ve
    de un golpe en qué coinciden AV, Iwan Baan y Wikidata). Dentro de una misma fuente no se junta
    nada: cada una ya sabe distinguir sus obras."""
    out = []
    for c in cands:
        hit = None
        for o in out:
            if c["source"] in o["sources"]:
                continue
            la, lo = (c["lat"], c["lon"]) if c["precision"] in EXACT else (None, None)
            ob, oo = (o["lat"], o["lon"]) if o["precision"] in EXACT else (None, None)
            if same_building(fold, c["name"], ", ".join(c["architects"]), la, lo,
                             o["name"], ", ".join(o["architects"]), ob, oo):
                hit = o
                break
        if hit is None:
            out.append(dict(c, sources=[c["source"]]))
            continue
        hit["sources"].append(c["source"])
        # lo que falte, de quien lo tenga; y gana la posición más precisa
        for k in ("thumb", "photo", "photo_title", "photo_page", "year", "city", "country", "url_av"):
            if not hit.get(k) and c.get(k):
                hit[k] = c[k]
        if hit["precision"] not in EXACT and c["precision"] in EXACT:
            hit.update(lat=c["lat"], lon=c["lon"], precision="exacta")
        for a in c["architects"]:
            if a not in hit["architects"]:
                hit["architects"].append(a)
        hit.setdefault("also", []).append({"source": c["source"], "name": c["name"], "url": c["url"]})
    return out


# ------------------------------------------------------------------------ descubrir

def discover(payload, hours_env=ENVELOPE_H, hours_halo=HALO_H, include_posible=False,
             sources=SOURCES, cache_dir=None, geocode=None, limit=400, budget=IWAN_GEOCODE_BUDGET):
    """-> {candidates, area}. Los candidatos salen ordenados por lo cerca que caen."""
    hours_env = min(float(hours_env), MAX_HOURS)
    hours_halo = min(float(hours_halo), MAX_HOURS)
    sources = [s for s in SOURCES if s in (sources or SOURCES)]
    stops, landmarks = payload["stops"], payload["landmarks"]
    pts = centres(stops, landmarks, include_posible)
    line = road_line(stops) if hours_halo > 0 else []
    r_env, r_halo = radius_km(hours_env), radius_km(hours_halo)
    area = {"hours_env": hours_env, "hours_halo": hours_halo, "centres": len(pts), "sources": sources,
            "radius_env_km": round(r_env, 1), "radius_halo_km": round(r_halo, 1),
            "found": {}, "errors": {}, "places_left": 0}
    if not pts and not line:
        return {"candidates": [], "area": dict(area, empty=True)}

    box = bbox(pts, line, max(r_env, r_halo))
    area["box"] = [round(v, 3) for v in box]
    # la geometría, para poder pintarla en el mapa tal como se ha medido
    area["centre_points"] = [[round(p[0], 4), round(p[1], 4)] for p in pts]
    area["road"] = [[round(p[0], 4), round(p[1], 4)] for p in line]
    hints = {s.get("city") for s in stops} | {s.get("country") for s in stops} \
        | {lm.get("city") for lm in landmarks}
    raw = []
    for src in sources:
        try:
            if src == "arquitecturaviva":
                got = av_candidates(box, cache_dir)
            elif src == "iwanbaan":
                got, area["places_left"] = iwan_candidates(box, cache_dir, geocode, budget,
                                                           {h for h in hints if h})
            else:
                got = wikidata_architecture(box, cache_dir)
        except Exception as exc:                     # una fuente caída no tumba a las demás
            area["errors"][src] = str(exc)[:160]
            continue
        area["found"][src] = len(got)
        raw += got

    seen = known(landmarks)
    located_stops = [s for s in stops if s.get("lat") is not None]
    out = []
    for cand in raw:
        env_km = nearest_km(cand["lat"], cand["lon"], pts) if pts else None
        halo_km = line_km(cand["lat"], cand["lon"], line) if line else None
        if cand.get("precision") == "pais":
            zone = "pais"                       # está en la zona, pero no se sabe dónde
        elif env_km is not None and env_km <= r_env:
            zone = "envolvente"
        elif halo_km is not None and halo_km <= r_halo:
            zone = "corredor"
        else:
            zone = None
        if zone is None or is_known(cand, seen):
            continue
        stop = min(located_stops, key=lambda s: haversine_km(cand["lat"], cand["lon"], s["lat"], s["lon"]),
                   default=None) if zone != "pais" else None
        km = haversine_km(cand["lat"], cand["lon"], stop["lat"], stop["lon"]) if stop else None
        out.append(dict(cand, zone=zone,
                        architect=", ".join(cand["architects"]) or "Sin arquitecto en la fuente",
                        stop_city=stop["city"] if stop else None,
                        drive_minutes=round(drive_minutes(km)) if km is not None else None,
                        near_km=round(env_km if zone == "envolvente" else halo_km, 1)
                        if zone in ("envolvente", "corredor") else None))
    out = merge_sources(out, seen["fold"])
    out.sort(key=lambda c: (-len(c["sources"]), c["drive_minutes"] if c["drive_minutes"] is not None else 1e9,
                            c["name"]))
    area["candidates"] = len(out)
    # Wikidata puede traer miles y taparlo todo: se recorta solo ella, y las listas curadas
    # entran enteras aunque queden al final del orden (los de «país» no tienen tiempo en coche)
    curated = [c for c in out if set(c["sources"]) & set(CURATED)]
    quota = limit - len(curated)
    shown = []
    for c in out:                      # sin alterar el orden: solo se cae lo que sobra
        if set(c["sources"]) & set(CURATED):
            shown.append(c)
        elif quota > 0:
            shown.append(c)
            quota -= 1
    area["shown"] = len(shown)
    area["trimmed"] = len(out) - len(shown)
    return {"candidates": shown, "area": area}
