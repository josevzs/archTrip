"""Autocompletar: la envolvente de 2 h, el corredor de 1 h, las tres fuentes y la importación.

Ninguna fuente se toca de verdad (la de Wikidata tarda medio minuto y la de Iwan Baan son siete
páginas): `fake_sources` sustituye la única puerta de salida (`discover._get`) por datos a mano
con la forma exacta que devuelve cada sitio, así que el parseo también queda probado. El viaje
de las pruebas va de Oporto (41,15/-8,61) a Lisboa (38,72/-9,14)."""
import pytest

from archtrip import discover
from conftest import LANDMARK_HEADER, LANDMARK_ROWS, ROUTE_HEADER, ROUTE_ROWS, make_xlsx, upload
from test_api import enrich_all, fake_geo, new_trip  # noqa: F401  (fixture + helper)

# --- Arquitectura Viva: su JSON de obras, tal cual ------------------------------
AV_RAW = [
    {"id": "39088", "slug": "casa-de-serralves", "title": "Casa de Serralves, Oporto", "img": "av_imagen.webp",
     "author": ["Álvaro Siza"], "country": ["Portugal"], "city": ["Oporto"], "coords": "41.16,-8.66",
     "hash": "5353879a", "date": "1999"},
    {"id": "39100", "slug": "convento-de-cristo", "title": "Convento de Cristo, Tomar", "img": "av_imagen.webp",
     "author": ["Diogo de Arruda"], "country": ["Portugal"], "city": ["Tomar"], "coords": "39.60,-8.42",
     "hash": None, "date": "1160"},
    {"id": "39200", "slug": "catedral-de-sevilla", "title": "Catedral de Sevilla", "img": None,
     "author": ["Varios"], "country": ["España"], "city": ["Sevilla"], "coords": "37.38,-5.99", "date": "1506"},
    {"id": "39300", "slug": "sin-coordenadas", "title": "Obra sin situar", "img": None, "author": [],
     "country": ["Portugal"], "city": [], "coords": "", "date": None},
]

# --- Iwan Baan: su mapa (coordenadas + foto) y el índice de WordPress (arquitectos) ---
def _img(slug):
    base = "https://iwan.com/wp-content/uploads/2020/01/" + slug
    return {"src": base + "-320x0-c-default.jpg", "attr": {"width": 320, "height": 213},
            "srcset": base + "-150x0-c-default.jpg 150w, " + base + "-320x0-c-default.jpg 320w, "
                      + base + "-750x0-c-default.jpg 750w"}


IWAN_MAP_RAW = [
    {"id": 100, "title": "Casa das Artes &#8211; Souto de Moura", "link": "https://iwan.com/portfolio/casa-das-artes/",
     "lat": 41.158, "lng": -8.628, "img": _img("casa-das-artes")},
    {"id": 101, "title": "Torre sem Cidade &#8211; Anónimo", "link": "https://iwan.com/portfolio/torre-sem-cidade/",
     "lat": 38.9, "lng": -9.0, "img": _img("torre")},
    {"id": 102, "title": "Vitra Campus &#8211; Herzog &#038; de Meuron", "link": "https://iwan.com/portfolio/vitra/",
     "lat": 47.601, "lng": 7.618, "img": _img("vitra")},
    {"id": 103, "title": "Sin coordenadas", "link": "https://iwan.com/portfolio/sin-coordenadas/",
     "lat": None, "lng": None, "img": {}},
]


IWAN_RAW = {
    "places": [{"id": 1, "name": "Porto", "count": 2}, {"id": 2, "name": "Portugal", "count": 3},
               {"id": 3, "name": "Germany", "count": 1}],
    "architects": [{"id": 10, "name": "Eduardo Souto de Moura"}, {"id": 11, "name": "Souto de Moura Arquitectos"},
                   {"id": 12, "name": "Herzog &amp; de Meuron"}],
    "jetpack-portfolio": [
        {"id": 100, "slug": "casa-das-artes-souto-de-moura", "link": "https://iwan.com/portfolio/casa-das-artes/",
         "title": {"rendered": "Casa das Artes &#8211; Souto de Moura"}, "architects": [10, 11],
         "places": [1, 2], "date": "1991-01-01T00:00:00"},
        {"id": 101, "slug": "torre-sem-cidade", "link": "https://iwan.com/portfolio/torre-sem-cidade/",
         "title": {"rendered": "Torre sem Cidade &#8211; Anónimo"}, "architects": [], "places": [2],
         "date": "2015-01-01T00:00:00"},
        {"id": 102, "slug": "vitra", "link": "https://iwan.com/portfolio/vitra/",
         "title": {"rendered": "Vitra Campus &#8211; Herzog &#038; de Meuron"}, "architects": [12],
         "places": [3], "date": "2010-01-01T00:00:00"},
    ],
}


# --- Wikidata: la respuesta del SPARQL -----------------------------------------
def _wd(qid, label, lat, lon, arch, place=None, year=None, image=None):
    row = {"item": {"value": "http://www.wikidata.org/entity/" + qid}, "itemLabel": {"value": label},
           "coord": {"value": "Point(%s %s)" % (lon, lat)}, "architectLabel": {"value": arch}}
    if place:
        row["placeLabel"] = {"value": place}
    if year:
        row["inception"] = {"value": year + "-01-01T00:00:00Z"}
    if image:
        row["image"] = {"value": image}
    return row


WD_RAW = {"results": {"bindings": [
    # el mismo convento que publica AV, en el mismo punto: un solo candidato con dos fuentes
    _wd("Q6", "Convento of Christ", 39.60, -8.42, "Diogo de Arruda", "Tomar", "1160"),
    _wd("Q5", "Monasterio de los Jerónimos", 38.6979, -9.2065, "Diogo de Boitaca", "Lisboa", "1601",
        "http://commons.wikimedia.org/wiki/Special:FilePath/Jer%C3%B3nimos.jpg"),
    _wd("Q5", "Monasterio de los Jerónimos", 38.6979, -9.2065, "Juan de Castilla", "Lisboa", "1601"),
    # Casa da Música ya está en el viaje (la sube la plantilla de pruebas)
    _wd("Q4", "Casa da Música", 41.17, -8.63, "Rem Koolhaas", "Oporto", "2005"),
    _wd("Q7", "Catedral de Sevilla", 37.38, -5.99, "Varios", "Sevilla"),
]}}

class FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


@pytest.fixture
def fake_sources(monkeypatch, tmp_path):
    """Las tres fuentes, sin red. Devuelve el directorio de caché (vacío) para poder comprobar
    que lo descargado se guarda."""
    def fake_get(url, params=None, timeout=60, accept=None):
        if "arquitecturaviva" in url:
            return FakeResp(AV_RAW)
        if "iwan.com/map" in url:
            return FakeResp(IWAN_MAP_RAW)
        if "iwan.com" in url:
            path = url.rstrip("/").rsplit("/", 1)[-1]
            return FakeResp(IWAN_RAW.get(path, []) if (params or {}).get("page", 1) == 1 else [])
        if "wikidata.org" in url:
            return FakeResp(WD_RAW)
        raise AssertionError("fuente inesperada en las pruebas: " + url)

    monkeypatch.setattr(discover, "_get", fake_get)
    monkeypatch.setattr(discover, "road_line", lambda stops, timeout=20:
                        [(s["lat"], s["lon"]) for s in stops if s.get("lat") is not None])
    return tmp_path / "cache"


# ------------------------------------------------------------------ geometría

def test_two_hours_of_driving_is_about_a_hundred_kilometres():
    # mismo criterio que estimate_drive: línea recta x1,3 a 70 km/h
    assert round(discover.radius_km(2)) == 108
    assert round(discover.radius_km(1)) == 54
    assert discover.radius_km(0) == 0
    assert round(discover.drive_minutes(54)) == 60


def test_the_envelope_grows_from_stops_and_from_what_is_already_curated():
    stops = [{"lat": 41.15, "lon": -8.61}, {"lat": None, "lon": None}]
    lms = [{"lat": 40.0, "lon": -8.0, "status": "curado"}, {"lat": 39.0, "lon": -8.0, "status": "posible"},
           {"lat": 38.0, "lon": -8.0, "status": "descartado"}, {"lat": None, "lon": None, "status": "curado"}]
    assert discover.centres(stops, lms) == [(41.15, -8.61), (40.0, -8.0)]
    assert len(discover.centres(stops, lms, include_posible=True)) == 3


def test_the_corridor_measures_the_detour_from_the_road_not_from_the_stops():
    road = [(41.0, -8.0), (40.0, -8.0)]          # un tramo norte-sur
    # un punto a mitad de camino está pegado a la carretera, aunque esté a 55 km de cada parada
    assert discover.line_km(40.5, -8.0, road) < 0.5
    assert round(discover.nearest_km(40.5, -8.0, road)) == 56
    # y uno desplazado al este está a su desvío real
    assert 40 < discover.line_km(40.5, -7.5, road) < 44
    assert discover.line_km(40.5, -8.0, []) is None
    assert round(discover.line_km(41.0, -8.5, [(41.0, -8.0)])) == 42   # una sola parada: distancia a ella


def test_the_query_box_covers_everything_plus_the_radius():
    south, west, north, east = discover.bbox([(41.15, -8.61)], [(38.72, -9.14)], 108)
    assert abs(north - (41.15 + 108 / 111.32)) < 0.01   # ~1 grado de latitud por cada 108 km
    assert abs(south - (38.72 - 108 / 111.32)) < 0.01
    assert west < -9.14 and east > -8.61
    assert discover.bbox([], [], 50) is None
    assert discover.in_box(40, -8, (39, -9, 41, -7)) and not discover.in_box(42, -8, (39, -9, 41, -7))


# -------------------------------------------------------------------- fuentes

def test_arquitectura_viva_gives_coordinates_a_thumbnail_and_the_page(fake_sources):
    got = discover.av_candidates((38.0, -10.0, 42.0, -7.0), fake_sources)
    assert [c["name"] for c in got] == ["Casa de Serralves, Oporto", "Convento de Cristo, Tomar"]
    serralves = got[0]
    assert (serralves["lat"], serralves["lon"]) == (41.16, -8.66)
    assert serralves["architects"] == ["Álvaro Siza"] and serralves["year"] == "1999"
    assert serralves["city"] == "Oporto" and serralves["country"] == "Portugal"
    assert serralves["precision"] == "exacta"
    assert serralves["url_av"] == "https://arquitecturaviva.com/obras/casa-de-serralves"
    assert serralves["thumb"] == ("https://arquitecturaviva.com/assets/uploads/obras/39088/"
                                  "av_thumb__av_imagen.webp?h=5353879a")
    assert got[1]["thumb"].endswith("av_thumb__av_imagen.webp")      # sin hash, sin ?h=
    assert (fake_sources / "arquitecturaviva.json").exists()         # se queda para la próxima


def test_iwan_baan_comes_from_its_own_map_with_coordinates_and_a_photo(fake_sources):
    got = discover.iwan_candidates((38.0, -10.0, 42.0, -7.0), fake_sources)
    casa = next(c for c in got if c["ref"] == "100")
    assert casa["name"] == "Casa das Artes"                      # el guion largo parte el título
    assert casa["architects"] == ["Eduardo Souto de Moura"]      # del índice, sin repetir el estudio
    assert (casa["lat"], casa["lon"]) == (41.158, -8.628) and casa["precision"] == "exacta"
    assert casa["city"] == "Porto" and casa["year"] == "1991"
    assert casa["thumb"].endswith("-320x0-c-default.jpg")        # la de la lista
    assert casa["photo"].endswith("-750x0-c-default.jpg")        # la mayor del srcset, para la ficha
    assert casa["photo_page"] == casa["url"] == "https://iwan.com/portfolio/casa-das-artes/"
    # el de fuera del rectángulo y el que no tiene coordenadas no salen
    assert [c["ref"] for c in got] == ["100", "101"]
    sin_indice = next(c for c in got if c["ref"] == "101")
    assert sin_indice["architects"] == ["Anónimo"]               # del propio título


def test_wikidata_groups_architects_and_brings_the_commons_photo(fake_sources):
    got = discover.wikidata_architecture((38.0, -10.0, 42.0, -7.0), fake_sources)
    jeronimos = next(c for c in got if c["ref"] == "Q5")
    assert jeronimos["architects"] == ["Diogo de Boitaca", "Juan de Castilla"]   # dos filas, un edificio
    assert jeronimos["year"] == "1601" and jeronimos["city"] == "Lisboa"
    assert jeronimos["photo"].endswith("Jer%C3%B3nimos.jpg?width=1600")
    assert jeronimos["thumb"].endswith("?width=640")
    assert jeronimos["photo_title"] == "Jerónimos.jpg"
    assert jeronimos["photo_page"].startswith("https://commons.wikimedia.org/wiki/File:")
    assert next(c for c in got if c["ref"] == "Q7").get("photo") is None
    assert discover.commons_photo(None) == {}


# ------------------------------------------------------------------ selección

def payload(stops, landmarks=()):
    return {"stops": [dict(s) for s in stops], "landmarks": [dict(l) for l in landmarks]}


STOPS = [{"lat": 41.15, "lon": -8.61, "city": "Oporto", "country": "Portugal"},
         {"lat": 38.72, "lon": -9.14, "city": "Lisboa", "country": "Portugal"}]


def test_near_things_come_in_far_things_do_not(fake_sources):
    out = discover.discover(payload(STOPS), cache_dir=fake_sources)
    zones = {c["name"]: c["zone"] for c in out["candidates"]}
    assert zones["Casa de Serralves, Oporto"] == "envolvente"      # a 5 km de Oporto
    assert zones["Convento de Cristo, Tomar"] == "corredor"        # de camino, con desvío
    assert zones["Casa das Artes"] == "envolvente"                 # la de Iwan Baan, en Oporto
    assert "Catedral de Sevilla" not in zones                      # a 400 km de todo
    serralves = next(c for c in out["candidates"] if c["name"].startswith("Casa de Serralves"))
    assert serralves["stop_city"] == "Oporto" and serralves["drive_minutes"] < 15
    assert serralves["architect"] == "Álvaro Siza"
    assert out["area"]["radius_env_km"] == 107.7
    assert out["area"]["found"] == {"arquitecturaviva": 2, "iwanbaan": 2, "wikidata": 4}
    assert out["area"]["errors"] == {}
    # lo que coincide en dos listas va primero; el resto, por lo cerca que cae
    assert len(out["candidates"][0]["sources"]) == 2
    resto = [c["drive_minutes"] for c in out["candidates"][1:]]
    assert resto == sorted(resto)


def test_the_same_building_in_two_sources_is_one_candidate_that_cites_both(fake_sources):
    out = discover.discover(payload(STOPS), cache_dir=fake_sources)
    convento = next(c for c in out["candidates"] if c["name"] == "Convento de Cristo, Tomar")
    assert convento["sources"] == ["arquitecturaviva", "wikidata"]      # mismo punto, dos listas
    assert convento["also"][0]["name"] == "Convento of Christ"
    assert convento["url_av"].endswith("/obras/convento-de-cristo")
    assert out["candidates"][0] is convento                            # lo que coincide, primero


def test_wikidata_is_trimmed_first_never_the_curated_lists(fake_sources):
    out = discover.discover(payload(STOPS), cache_dir=fake_sources, limit=2)
    a = out["area"]
    assert (a["candidates"], a["shown"], a["trimmed"]) == (6, 4, 2)
    # los cuatro de las listas curadas entran aunque el tope sea 2; se cae lo que solo tiene Wikidata
    assert not [c for c in out["candidates"] if c["sources"] == ["wikidata"]]
    assert {"Casa das Artes", "Torre sem Cidade"} <= {c["name"] for c in out["candidates"]}
    # y el orden no cambia por recortar
    todo = discover.discover(payload(STOPS), cache_dir=fake_sources)["candidates"]
    assert [c["name"] for c in out["candidates"]] == [c["name"] for c in todo if c["sources"] != ["wikidata"]]


def test_only_the_sources_asked_for(fake_sources):
    out = discover.discover(payload(STOPS), sources=["arquitecturaviva"], cache_dir=fake_sources)
    assert set(out["area"]["found"]) == {"arquitecturaviva"}
    assert all(c["sources"] == ["arquitecturaviva"] for c in out["candidates"])


def test_a_source_that_is_down_does_not_take_the_others_with_it(fake_sources, monkeypatch):
    real = discover._get

    def flaky(url, params=None, timeout=60, accept=None):
        if "wikidata" in url:
            raise RuntimeError("504 gateway timeout")
        return real(url, params, timeout, accept)

    monkeypatch.setattr(discover, "_get", flaky)
    out = discover.discover(payload(STOPS), cache_dir=fake_sources)
    assert "wikidata" in out["area"]["errors"] and out["area"]["found"]["arquitecturaviva"] == 2
    assert out["candidates"]


def test_without_a_corridor_what_is_only_on_the_way_drops_out(fake_sources):
    out = discover.discover(payload(STOPS), hours_halo=0, cache_dir=fake_sources)
    assert "Convento de Cristo, Tomar" not in [c["name"] for c in out["candidates"]]


def test_what_the_trip_already_has_is_not_proposed_again(fake_sources):
    mine = [
        # por el nombre tal cual lo escribe la fuente
        {"name": "Casa de Serralves, Oporto", "architect": "x", "name_key": "casa de serralves, oporto|x",
         "wikidata_id": None, "lat": 41.16, "lon": -8.66, "status": "posible"},
        # por coincidir en el punto, aunque el nombre sea otro
        {"name": "Mosteiro", "architect": "", "name_key": "mosteiro|", "wikidata_id": None,
         "lat": 39.6001, "lon": -8.4201, "status": "posible"},
        # por el identificador de Wikidata
        {"name": "Jerónimos", "architect": "", "name_key": "jeronimos|", "wikidata_id": "Q5",
         "lat": None, "lon": None, "status": "posible"},
        # y por el nombre, aunque las coordenadas de cada fuente bailen unos metros
        {"name": "Torre sem Cidade", "architect": "", "name_key": "torre sem cidade|", "wikidata_id": None,
         "lat": 38.901, "lon": -9.001, "status": "posible"},
    ]
    out = discover.discover(payload(STOPS, mine), cache_dir=fake_sources)
    # queda lo que no está en la lista de arriba: la Casa da Música de Wikidata y la de Iwan Baan
    assert sorted(c["name"] for c in out["candidates"]) == ["Casa da Música", "Casa das Artes"]


def test_the_same_author_two_streets_away_is_flagged_not_hidden(fake_sources):
    """El viaje está en inglés y las fuentes en español: «Casa das Artes» y «House of Arts» no se
    reconocen por el nombre, así que se avisa por autor y cercanía."""
    mine = [
        {"name": "Convent of Christ", "architect": "D. de Arruda y otros", "city": "Tomar",
         "name_key": "convent of christ|d. de arruda y otros", "wikidata_id": None,
         "lat": 39.605, "lon": -8.425, "status": "descartado"},
        {"name": "House of Arts", "architect": "Eduardo Souto de Moura Arquitectos", "city": "Oporto",
         "name_key": "house of arts|eduardo souto de moura arquitectos", "wikidata_id": None,
         "lat": 41.160, "lon": -8.630, "status": "posible"},
    ]
    out = discover.discover(payload(STOPS, mine), cache_dir=fake_sources)
    convento = next(c for c in out["candidates"] if c["name"].startswith("Convento"))
    assert convento["dup_hint"] == {"name": "Convent of Christ", "status": "descartado", "km": 0.7}
    casa = next(c for c in out["candidates"] if c["name"] == "Casa das Artes")
    assert casa["dup_hint"] == {"name": "House of Arts", "status": "posible", "km": 0.3}
    # y los que no se parecen a nada no llevan aviso
    assert next(c for c in out["candidates"] if c["name"] == "Casa da Música")["dup_hint"] is None


def test_a_trip_without_coordinates_finds_nothing_and_says_so(fake_sources):
    out = discover.discover(payload([{"lat": None, "lon": None, "city": "Oporto"}]), cache_dir=fake_sources)
    assert out["candidates"] == [] and out["area"]["empty"] is True


# ----------------------------------------------------------------------- API

def seed(client):
    tid = new_trip(client)
    upload(client, f"/api/trips/{tid}/route", make_xlsx(ROUTE_HEADER, ROUTE_ROWS))
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER, LANDMARK_ROWS))
    enrich_all(client, tid)
    return tid


@pytest.fixture
def api_sources(fake_sources):
    return fake_sources


def test_discover_endpoint_lists_candidates_without_touching_the_trip(client, fake_geo, api_sources):
    tid = seed(client)
    before = client.get(f"/api/trips/{tid}").get_json()["landmarks"]
    r = client.post(f"/api/trips/{tid}/discover", json={})
    assert r.status_code == 200
    out = r.get_json()
    assert out["area"]["hours_env"] == 2.0 and out["area"]["hours_halo"] == 1.0
    names = [c["name"] for c in out["candidates"]]
    # Serralves y Casa da Música ya están en el viaje (la plantilla los sube): no se repiten
    assert "Casa da Música" not in names and not any(n.startswith("Casa de Serralves") for n in names)
    assert "Convento de Cristo, Tomar" in names and "Casa das Artes" in names
    assert client.get(f"/api/trips/{tid}").get_json()["landmarks"] == before
    assert client.post(f"/api/trips/{tid}/discover", json={"hours_env": "dos"}).status_code == 400
    assert client.post("/api/trips/999/discover", json={}).status_code == 404


def test_every_source_down_is_a_message_not_a_crash(client, fake_geo, monkeypatch):
    tid = seed(client)
    monkeypatch.setattr(discover, "road_line", lambda stops, timeout=20: [])

    def boom(*a, **kw):
        raise RuntimeError("sin red")
    monkeypatch.setattr(discover, "bbox", boom)
    r = client.post(f"/api/trips/{tid}/discover", json={})
    assert r.status_code == 502 and "fuente" in r.get_json()["error"]


def test_importing_a_candidate_leaves_it_as_posible_with_its_links(client, fake_geo, api_sources):
    tid = seed(client)
    cands = client.post(f"/api/trips/{tid}/discover", json={}).get_json()["candidates"]
    convento = next(c for c in cands if c["name"] == "Convento de Cristo, Tomar")
    r = client.post(f"/api/trips/{tid}/discover/import", json={"items": [convento]})
    assert r.status_code == 201
    body = r.get_json()
    assert [a["name"] for a in body["added"]] == ["Convento de Cristo, Tomar"] and body["skipped"] == []
    assert body["added"][0]["status"] == "posible" and "images" in body["added"][0]

    lm = next(l for l in client.get(f"/api/trips/{tid}").get_json()["landmarks"] if l["name"].startswith("Convento"))
    assert (lm["status"], lm["geocode_status"]) == ("posible", "exacta")
    assert (lm["lat"], lm["lon"]) == (39.60, -8.42) and lm["city"] == "Tomar" and lm["year"] == "1160"
    assert lm["url_av"] == "https://arquitecturaviva.com/obras/convento-de-cristo"
    assert "Arquitectura Viva" in lm["notes"] and "Wikidata" in lm["notes"]
    assert lm["drive_source"] is None              # el tiempo en coche lo pone el enriquecido
    assert body["pending"]["drive"] >= 1

    # importar lo mismo otra vez no duplica
    again = client.post(f"/api/trips/{tid}/discover/import", json={"items": [convento]}).get_json()
    assert again["added"] == [] and again["skipped"][0]["why"] == "ya estaba en el viaje"
    assert client.post(f"/api/trips/{tid}/discover/import", json={"items": []}).status_code == 400


def test_a_candidate_from_iwan_baan_brings_its_photo_and_its_page(client, fake_geo, api_sources):
    tid = seed(client)
    cands = client.post(f"/api/trips/{tid}/discover", json={}).get_json()["candidates"]
    casa = next(c for c in cands if c["name"] == "Casa das Artes")
    client.post(f"/api/trips/{tid}/discover/import", json={"items": [casa]})
    lm = next(l for l in client.get(f"/api/trips/{tid}").get_json()["landmarks"] if l["name"] == "Casa das Artes")
    assert (lm["lat"], lm["lon"], lm["geocode_status"]) == (41.158, -8.628, "exacta")
    assert "iwan.com" in lm["notes"] and lm["images_status"] == "ok"
    assert lm["images"][0]["source"] == "iwanbaan" and lm["images"][0]["url"].endswith("-750x0-c-default.jpg")
    assert lm["images"][0]["page_url"] == "https://iwan.com/portfolio/casa-das-artes/"


def test_a_wikidata_photo_comes_with_the_landmark(client, fake_geo, api_sources):
    tid = seed(client)
    cands = client.post(f"/api/trips/{tid}/discover", json={}).get_json()["candidates"]
    jeronimos = next(c for c in cands if c["name"].startswith("Monasterio"))
    client.post(f"/api/trips/{tid}/discover/import", json={"items": [jeronimos]})
    lm = next(l for l in client.get(f"/api/trips/{tid}").get_json()["landmarks"] if l["wikidata_id"] == "Q5")
    assert lm["images_status"] == "ok" and len(lm["images"]) == 1
    assert lm["images"][0]["source"] == "wikidata" and "Jer" in lm["images"][0]["title"]
    # y el hito entero vuelve en la respuesta, para pintarlo sin recargar el viaje
    added = client.post(f"/api/trips/{tid}/discover/import",
                        json={"items": [c for c in cands if c["name"] == "Casa das Artes"]}).get_json()["added"]
    assert added[0]["name"] == "Casa das Artes" and added[0]["images"] and added[0]["nearest_stop_city"] is None


def test_an_import_is_one_entry_in_the_journal_and_undoes_in_one_click(client, fake_geo, api_sources):
    tid = seed(client)
    cands = client.post(f"/api/trips/{tid}/discover", json={}).get_json()["candidates"]
    client.post(f"/api/trips/{tid}/discover/import", json={"items": cands})
    assert len(client.get(f"/api/trips/{tid}").get_json()["landmarks"]) == len(LANDMARK_ROWS) + len(cands)

    client.post("/api/admin/login", json={"password": "admin"})
    sid = client.get("/api/admin/sessions").get_json()[0]["id"]
    journal = client.get(f"/api/admin/sessions/{sid}/changes").get_json()
    change = next(c for c in journal if c["action"] == "bulk_create")
    assert change["revertible"]
    assert client.post(f"/api/admin/changes/{change['id']}/revert").status_code == 200
    assert len(client.get(f"/api/trips/{tid}").get_json()["landmarks"]) == len(LANDMARK_ROWS)
