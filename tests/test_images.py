import pytest

from archtrip import images


def test_is_drawing_and_file_url():
    assert images.is_drawing("Katsura-Plan.jpg") and images.is_drawing("Grundriss EG.png")
    assert images.is_drawing("平面図.jpg") and images.is_drawing("Section A-A.svg")
    assert not images.is_drawing("Katsura Gepparo.jpg")
    assert images.is_drawing("Site layout.png", "image/png") and not images.is_drawing("Facade.jpg", "image/jpeg")
    assert not images.is_drawing("Old photo.png", "image/png")
    assert images.file_url("Katsura Plan.jpg", 640) == "https://commons.wikimedia.org/wiki/Special:FilePath/Katsura_Plan.jpg?width=640"


def _wd(lat=None, image=None, cat=None, plans=(), wiki=None, lon=135.0):
    claims = {}
    if lat is not None:
        claims["P625"] = [{"mainsnak": {"datavalue": {"value": {"latitude": lat, "longitude": lon}}}}]
    if image:
        claims["P18"] = [{"mainsnak": {"datavalue": {"value": image}}}]
    if cat:
        claims["P373"] = [{"mainsnak": {"datavalue": {"value": cat}}}]
    claims["P3311"] = [{"mainsnak": {"datavalue": {"value": p}}} for p in plans]
    sitelinks = {f"{wiki}wiki": {"title": "Casa X"}} if wiki else {}
    return {"claims": claims, "sitelinks": sitelinks}


@pytest.fixture
def fake_api(monkeypatch):
    calls = []

    def get(url, params):
        calls.append((url, params))
        if params.get("list") == "search":
            return {"query": {"search": []}}
        if params.get("action") == "wbsearchentities":
            if params["search"] != "Sky House":
                return {"search": []}
            return {"search": [
                {"id": "Q9", "label": "Sky House", "description": "album by Someone"},
                {"id": "Q1", "label": "Sky House", "description": "house in Oregon"},
                {"id": "Q2", "label": "Sky House", "description": "residence in Bunkyo, Tokyo"},
            ]}
        if params.get("action") == "wbgetentities":
            return {"entities": {"Q1": _wd(lat=45.0, image="Oregon.jpg"),
                                 "Q2": _wd(lat=35.7, lon=139.75, image="Sky House.jpg", cat="Sky House (Tokyo)",
                                           plans=["Sky House plan.png"], wiki="ja")}}
        if params.get("generator") == "categorymembers":
            return {"query": {"pages": {
                "1": {"title": "File:Sky House 1.jpg", "index": 1, "imageinfo": [{"mime": "image/jpeg", "thumburl": "https://t/1.jpg", "descriptionurl": "https://c/File:Sky_House_1.jpg"}]},
                "2": {"title": "File:Sky House section.jpg", "index": 2, "imageinfo": [{"mime": "image/jpeg", "thumburl": "https://t/2.jpg"}]},
                "3": {"title": "File:Sky House.jpg", "index": 3, "imageinfo": [{"mime": "image/jpeg", "thumburl": "https://t/3.jpg"}]},
                "4": {"title": "File:Sky House.ogv", "index": 4, "imageinfo": [{"mime": "video/ogg", "thumburl": "https://t/4.jpg"}]},
                "7": {"title": "File:Sky House book.djvu", "index": 5, "imageinfo": [{"mime": "image/vnd.djvu", "thumburl": "https://t/7.jpg"}]},
            }}}
        if params.get("generator") == "search":
            return {"query": {"pages": {"5": {"title": "File:Sky House elevation.png", "index": 1,
                                              "imageinfo": [{"mime": "image/png", "thumburl": "https://t/5.png"}]},
                                        "6": {"title": "File:Sky House facade.jpg", "index": 2,
                                              "imageinfo": [{"mime": "image/jpeg", "thumburl": "https://t/6.jpg"}]}}}}
        raise AssertionError(params)

    monkeypatch.setattr(images, "_get", get)
    return calls


def test_wikidata_lookup_prefers_nearby_then_building_words(fake_api):
    info = images.wikidata_lookup("Sky House", 35.71, 139.75)
    assert info["id"] == "Q2" and info["category"] == "Sky House (Tokyo)" and info["plans"] == ["Sky House plan.png"]
    assert info["wikipedia"] == "https://ja.wikipedia.org/wiki/Casa_X"
    # no coordinates known: the album is skipped, the first place-like description wins
    assert images.wikidata_lookup("Sky House")["id"] == "Q1"


def test_fetch_images_assembles_photos_and_drawings(fake_api):
    r = images.fetch_images("Sky House", "Tokio", 35.71, 139.75)
    assert r["wikidata_id"] == "Q2" and r["lat"] == 35.7
    kinds = [(im["kind"], im["title"]) for im in r["images"]]
    assert kinds.count(("foto", "Sky House.jpg")) == 1        # P18, deduplicated with the category copy
    assert ("foto", "Sky House 1.jpg") in kinds
    assert ("plano", "Sky House plan.png") in kinds          # P3311
    assert ("plano", "Sky House section.jpg") in kinds       # classified by title
    assert ("plano", "Sky House elevation.png") in kinds     # in-category drawings search
    assert not any("ogv" in t or "djvu" in t for _, t in kinds)   # videos and book scans skipped
    assert ("foto", "Sky House facade.jpg") in kinds         # drawings search hit that is really a photo
    assert "mime" not in r["images"][0]
    assert r["images"][0]["url"].startswith("https://commons.wikimedia.org/wiki/Special:FilePath/")
    assert r["images"][0]["source"] == "wikidata"


def test_retry_after_429(monkeypatch):
    class Resp:
        def __init__(self, code): self.status_code = code; self.headers = {"Retry-After": "1"}
        def raise_for_status(self): pass
        def json(self): return {"ok": True}
    seq = iter([Resp(429), Resp(200)])
    monkeypatch.setattr(images.requests, "get", lambda *a, **k: next(seq))
    slept = []
    monkeypatch.setattr(images.time, "sleep", lambda s: slept.append(s))
    assert images._get(images.COMMONS_API, {"action": "query"}) == {"ok": True}
    assert 1 in slept


def test_core_name():
    assert images.core_name("Museo de Serralves") == "Serralves"
    assert images.core_name("Osaka Prefectural Nakanoshima Library") == "Osaka Nakanoshima"
    assert images.core_name("Serralves") is None
    assert images.core_name("Casa da Música") == "da Música"


def test_lookup_falls_back_to_fulltext_then_core(monkeypatch):
    calls = []

    def get(url, params):
        calls.append(params)
        if params.get("action") == "wbsearchentities":
            if params["search"] == "Serralves":
                return {"search": [{"id": "Q1", "label": "Serralves Museum", "description": "art museum in Porto"}]}
            return {"search": []}
        if params.get("list") == "search":
            return {"query": {"search": [{"title": "Q9", "snippet": "<b>exhibition</b> at Serralves"}]}}
        if params.get("action") == "wbgetentities":
            return {"entities": {"Q1": _wd(lat=41.16, lon=-8.66, image="Serralves.jpg")}}
        raise AssertionError(params)

    monkeypatch.setattr(images, "_get", get)
    info = images.wikidata_lookup("Museo de Serralves", 41.15, -8.61)
    assert info and info["id"] == "Q1"
    assert [c.get("search") or c.get("srsearch") for c in calls if c.get("action") != "wbgetentities"] == \
        ["Museo de Serralves", "Museo de Serralves", "Museo de Serralves", "Serralves"]
    # far away from the landmark: the strict last resort refuses
    assert images.wikidata_lookup("Museo de Serralves", 35.0, 139.0) is None


def test_far_namesake_is_rejected_even_if_it_looks_like_a_building(monkeypatch):
    def get(url, params):
        if params.get("action") == "wbsearchentities":
            return {"search": [{"id": "Q7", "label": "St. Anselm Church", "description": "church in Lancashire"}]}                 if params["search"] == "St. Anselm Church" else {"search": []}
        if params.get("list") == "search":
            return {"query": {"search": []}}
        if params.get("action") == "wbgetentities":
            return {"entities": {"Q7": _wd(lat=53.66, lon=-2.17, image="uk.jpg")}}
        raise AssertionError(params)
    monkeypatch.setattr(images, "_get", get)
    assert images.wikidata_lookup("St. Anselm Church", 35.68, 139.76) is None     # Tokyo landmark, UK entity
    assert images.wikidata_lookup("St. Anselm Church")["id"] == "Q7"             # nothing known: accepted


def test_entities_that_are_not_buildings_are_rejected(monkeypatch):
    """Casos reales que colaron fotos absurdas: la revista House & Garden para 'Garden & House',
    el artículo 'casa' para 'House NA', el emperador Go-Kōmyō para 'Kōmyō-in' y el análisis de
    series temporales para TIME'S. Ninguna de esas fichas tiene coordenadas."""
    hits = {
        "Garden & House": [{"id": "Q1", "label": "House & Garden", "description": "American magazine"}],
        "House NA": [{"id": "Q2", "label": "casa", "description": "building for human habitation"}],
        "Kōmyō-in": [{"id": "Q3", "label": "Emperor Go-Kōmyō", "description": "Emperor of Japan"}],
        "TIME'S": [{"id": "Q4", "label": "time series analysis", "description": "statistical technique"}],
        "Musashino Place": [{"id": "Q5", "label": "Musashino Place", "description": "public facility in Tokyo"}],
    }

    def get(url, params):
        if params.get("action") == "wbsearchentities":
            return {"search": hits.get(params["search"], [])}
        if params.get("list") == "search":
            return {"query": {"search": []}}
        if params.get("action") == "wbgetentities":
            return {"entities": {q: _wd(image=q + ".jpg") for q in params["ids"].split("|")}}   # sin coordenadas
        raise AssertionError(params)

    monkeypatch.setattr(images, "_get", get)
    for name in ("Garden & House", "House NA", "Kōmyō-in", "TIME'S"):
        assert images.wikidata_lookup(name) is None, name
    # y lo que sí coincide palabra por palabra sigue pasando
    assert images.wikidata_lookup("Musashino Place")["id"] == "Q5"


def test_city_nearby_is_not_the_building_but_a_district_landmark_is(monkeypatch):
    """La ficha de la ciudad cae al lado de todos sus edificios; solo vale si se llama igual
    que el hito, que es el caso de los barrios que sí son el hito (Gion, Higashi Chaya)."""
    hits = {
        "Musashino Place": [{"id": "Q1", "label": "Musashino", "description": "city in Tokyo, Japan"}],
        "Higashi Chaya District": [{"id": "Q2", "label": "Higashichaya", "description": "neighborhood in Kanazawa, Japan"}],
        "Casa de Serralves": [{"id": "Q3", "label": "Casa de Serralves", "description": "building in Porto, Porto District, Portugal"}],
    }

    def get(url, params):
        if params.get("action") == "wbsearchentities":
            return {"search": hits.get(params["search"], [])}
        if params.get("list") == "search":
            return {"query": {"search": []}}
        if params.get("action") == "wbgetentities":
            return {"entities": {q: _wd(lat=35.70, lon=139.54, image=q + ".jpg") for q in params["ids"].split("|")}}
        raise AssertionError(params)

    monkeypatch.setattr(images, "_get", get)
    assert images.wikidata_lookup("Musashino Place", 35.70, 139.54) is None
    assert images.wikidata_lookup("Higashi Chaya District", 35.70, 139.54)["id"] == "Q2"
    # el distrito de la dirección no convierte un edificio en un barrio
    assert images.wikidata_lookup("Casa de Serralves", 35.70, 139.54)["id"] == "Q3"
