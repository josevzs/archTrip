import io
import json
import zipfile

import pytest

from archtrip import geo, images, links
from conftest import LANDMARK_HEADER, LANDMARK_ROWS, ROUTE_HEADER, ROUTE_ROWS, make_xlsx, upload

COORDS = {
    "Oporto, Portugal": (41.1579, -8.6291),
    "Lisboa, Portugal": (38.7223, -9.1393),
    "Av. da Boavista 604, Oporto": (41.1587, -8.6307),
    "Casa das Histórias Paula Rego, Cascais": (38.6975, -9.4215),
}


@pytest.fixture
def fake_geo(monkeypatch):
    """Deterministic Nominatim/OSRM so tests never touch the network."""
    log = {"geocode": [], "osrm": []}

    def geocode(query, prefer_settlement=False):
        log["geocode"].append(query)
        return COORDS.get(query)

    def osrm(lat1, lon1, lat2, lon2):
        log["osrm"].append((lat1, lon1, lat2, lon2))
        return 42.0, 10.5

    def fetch_images(name, city, lat=None, lon=None):
        log["images"].append(name)
        return {"wikidata_id": None, "wikipedia_url": None, "lat": None, "lon": None, "images": []}

    log["images"] = []
    monkeypatch.setattr(links, "find_archdaily", lambda n, a, c="", k="": None)
    monkeypatch.setattr(links, "find_av", lambda n, a, c="": None)
    monkeypatch.setattr(geo, "nominatim_geocode", geocode)
    monkeypatch.setattr(geo, "osrm_drive", osrm)
    monkeypatch.setattr(images, "fetch_images", fetch_images)
    return log


def new_trip(client, name="Portugal 2027"):
    r = client.post("/api/trips", json={"name": name})
    assert r.status_code == 201
    return r.get_json()["id"]


def enrich_all(client, trip_id, limit=80):
    last = None
    for _ in range(limit):
        last = client.post(f"/api/trips/{trip_id}/enrich/next").get_json()
        if last["done"]:
            return last
    raise AssertionError(f"enrichment never finished: {last}")


def test_trip_crud(client):
    assert client.get("/api/trips").get_json() == []
    tid = new_trip(client)
    assert client.post("/api/trips", json={"name": "  "}).status_code == 400
    trips = client.get("/api/trips").get_json()
    assert trips[0]["name"] == "Portugal 2027" and trips[0]["landmark_count"] == 0
    assert client.patch(f"/api/trips/{tid}", json={"name": "Portugal 28"}).get_json()["name"] == "Portugal 28"
    assert client.delete(f"/api/trips/{tid}").status_code == 204
    assert client.get(f"/api/trips/{tid}").status_code == 404


def test_templates_download(client):
    for name in ("ruta", "hitos"):
        r = client.get(f"/api/templates/{name}.xlsx")
        assert r.status_code == 200 and r.data[:2] == b"PK"
        assert f"plantilla_{name}.xlsx" in r.headers["Content-Disposition"]


def test_upload_and_enrich(client, route_xlsx, landmarks_xlsx, fake_geo):
    tid = new_trip(client)
    r = upload(client, f"/api/trips/{tid}/route", route_xlsx)
    assert r.status_code == 200 and r.get_json() == {"added": 2, "errors": []}
    r = upload(client, f"/api/trips/{tid}/landmarks", landmarks_xlsx)
    assert r.get_json() == {"added": 3, "updated": 0, "errors": []}

    data = client.get(f"/api/trips/{tid}").get_json()
    # 2 stops + 2 landmarks to geocode (Serralves has coords) + 1 drive for Serralves once a stop is located
    assert data["pending"]["stops"] == 2 and data["pending"]["geocode"] == 2
    by_name = {lm["name"]: lm for lm in data["landmarks"]}
    assert by_name["Museo de Serralves"]["geocode_status"] == "manual"

    result = enrich_all(client, tid)
    assert result["remaining"] == 0
    data = client.get(f"/api/trips/{tid}").get_json()
    by_name = {lm["name"]: lm for lm in data["landmarks"]}
    assert all(s["geocode_status"] == "ok" for s in data["stops"])
    casa = by_name["Casa da Música"]
    assert casa["geocode_status"] == "exacta" and casa["nearest_stop_city"] == "Oporto"
    assert casa["drive_minutes"] == 42.0 and casa["drive_source"] == "osrm"
    assert by_name["Casa das Histórias Paula Rego"]["nearest_stop_city"] == "Lisboa"
    assert by_name["Museo de Serralves"]["drive_source"] == "osrm"
    # address-first query for Casa da Música, name-first for the others
    assert "Av. da Boavista 604, Oporto" in fake_geo["geocode"]


def test_geocode_falls_back_to_city_then_fails(client, fake_geo, monkeypatch):
    tid = new_trip(client)
    upload(client, f"/api/trips/{tid}/route", make_xlsx(ROUTE_HEADER, ROUTE_ROWS))
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER[:3], [
        ["Edificio desconocido", "Nadie", "Oporto"], ["Otro", "Nadie", "Ciudad inexistente"],
        ["Torre (con paréntesis), Boavista", "Nadie", "Oporto"]]))
    COORDS["Oporto"] = (41.15, -8.61)
    COORDS["Torre, Oporto"] = (41.17, -8.62)
    enrich_all(client, tid)
    lms = {lm["name"]: lm for lm in client.get(f"/api/trips/{tid}").get_json()["landmarks"]}
    assert lms["Edificio desconocido"]["geocode_status"] == "ciudad"
    assert lms["Torre (con paréntesis), Boavista"]["geocode_status"] == "exacta"   # simplified name hit
    assert "Torre, Oporto" in fake_geo["geocode"]
    assert lms["Edificio desconocido"]["drive_minutes"] == 42.0
    assert lms["Otro"]["geocode_status"] == "fallido" and lms["Otro"]["drive_minutes"] is None


def test_osrm_failure_uses_estimate(client, fake_geo, monkeypatch):
    monkeypatch.setattr(geo, "osrm_drive", lambda *a: (_ for _ in ()).throw(geo.requests.ConnectionError()))
    tid = new_trip(client)
    upload(client, f"/api/trips/{tid}/route", make_xlsx(ROUTE_HEADER, ROUTE_ROWS))
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER, [LANDMARK_ROWS[1]]))
    enrich_all(client, tid)
    lm = client.get(f"/api/trips/{tid}").get_json()["landmarks"][0]
    assert lm["drive_source"] == "estimado" and lm["drive_minutes"] > 0


def test_nominatim_outage_reports_error(client, fake_geo, monkeypatch):
    monkeypatch.setattr(geo, "nominatim_geocode", lambda q, **k: (_ for _ in ()).throw(geo.requests.ConnectionError()))
    tid = new_trip(client)
    upload(client, f"/api/trips/{tid}/route", make_xlsx(ROUTE_HEADER, ROUTE_ROWS))
    r = client.post(f"/api/trips/{tid}/enrich/next").get_json()
    assert r["done"] is False and "error" in r and r["remaining"] == 2


def test_status_survives_reupload_and_edits(client, fake_geo):
    tid = new_trip(client)
    upload(client, f"/api/trips/{tid}/route", make_xlsx(ROUTE_HEADER, ROUTE_ROWS))
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER, LANDMARK_ROWS))
    enrich_all(client, tid)
    lms = client.get(f"/api/trips/{tid}").get_json()["landmarks"]
    casa = next(lm for lm in lms if lm["name"] == "Casa da Música")

    assert client.patch(f"/api/landmarks/{casa['id']}", json={"status": "curado"}).get_json()["status"] == "curado"
    assert client.patch(f"/api/landmarks/{casa['id']}", json={"status": "lo que sea"}).status_code == 400

    # re-upload: same rows + one new, Casa da Música with a corrected address
    rows = [list(r) for r in LANDMARK_ROWS] + [["Piscina das Marés", "Álvaro Siza", "Leça da Palmeira"] + [None] * 9]
    rows[0][3] = "Avenida da Boavista 604-610"
    r = upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER, rows)).get_json()
    assert (r["added"], r["updated"]) == (1, 3)
    lms = {lm["name"]: lm for lm in client.get(f"/api/trips/{tid}").get_json()["landmarks"]}
    assert lms["Casa da Música"]["status"] == "curado"
    assert lms["Casa da Música"]["geocode_status"] == "pendiente"       # address changed -> re-geocode
    assert lms["Museo de Serralves"]["geocode_status"] == "manual"      # unchanged coords kept
    assert lms["Museo de Serralves"]["drive_minutes"] == 42.0
    assert lms["Piscina das Marés"]["status"] == "pendiente"

    # manual coordinates reset the drive time, retry_geocode clears them
    r = client.patch(f"/api/landmarks/{casa['id']}", json={"lat": "41,16", "lon": "-8.63"}).get_json()
    assert r["geocode_status"] == "manual" and r["lat"] == 41.16 and r["drive_source"] is None
    assert client.patch(f"/api/landmarks/{casa['id']}", json={"lat": "x", "lon": "1"}).status_code == 400
    r = client.patch(f"/api/landmarks/{casa['id']}", json={"retry_geocode": True}).get_json()
    assert r["lat"] is None and r["geocode_status"] == "pendiente"

    assert client.delete(f"/api/landmarks/{casa['id']}").status_code == 204
    assert client.delete(f"/api/trips/{tid}/landmarks").status_code == 204
    assert client.get(f"/api/trips/{tid}").get_json()["landmarks"] == []


def test_route_reupload_resets_drive_times(client, fake_geo):
    tid = new_trip(client)
    upload(client, f"/api/trips/{tid}/route", make_xlsx(ROUTE_HEADER, ROUTE_ROWS))
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER, [LANDMARK_ROWS[1]]))
    enrich_all(client, tid)
    upload(client, f"/api/trips/{tid}/route", make_xlsx(ROUTE_HEADER, [ROUTE_ROWS[1]]))
    lm = client.get(f"/api/trips/{tid}").get_json()["landmarks"][0]
    assert lm["drive_source"] is None and lm["nearest_stop_id"] is None
    enrich_all(client, tid)
    lm = client.get(f"/api/trips/{tid}").get_json()["landmarks"][0]
    assert lm["nearest_stop_city"] == "Lisboa"


def test_bad_uploads(client):
    tid = new_trip(client)
    assert client.post(f"/api/trips/{tid}/route").status_code == 400
    r = upload(client, f"/api/trips/{tid}/route", io.BytesIO(b"hola"), name="ruta.csv")
    assert r.status_code == 400 and "xlsx" in r.get_json()["error"]
    r = upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(["Edificio", "Ciudad"], [["a", "b"]]))
    assert r.status_code == 400 and "Arquitecto" in r.get_json()["errors"][0]


def test_exports(client, fake_geo):
    tid = new_trip(client, "Viaje: Portugal / 2027?")
    upload(client, f"/api/trips/{tid}/route", make_xlsx(ROUTE_HEADER, ROUTE_ROWS))
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER, LANDMARK_ROWS))
    enrich_all(client, tid)
    lm_id = client.get(f"/api/trips/{tid}").get_json()["landmarks"][0]["id"]
    client.patch(f"/api/landmarks/{lm_id}", json={"status": "curado", "notes": "cierra <lunes>"})

    r = client.get(f"/api/trips/{tid}/export/html")
    assert r.status_code == 200 and "viaje-viaje-portugal-2027.html" in r.headers["Content-Disposition"]
    html = r.data.decode("utf-8")
    assert '<script id="archtrip-data">window.__ARCHTRIP__ = ' in html
    assert "<!--ARCHTRIP_DATA-->" not in html and "</script>" in html
    start = html.index("window.__ARCHTRIP__ = ") + len("window.__ARCHTRIP__ = ")
    embedded = json.loads(html[start:html.index(";</script>", start)])
    assert embedded["trip"]["id"] == tid and len(embedded["landmarks"]) == 3 and embedded["exported_at"]
    assert "cierra \\u003clunes>" in html            # '<' escaped so JSON can't close the script tag

    r = client.get(f"/api/trips/{tid}/export/obsidian")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.data))
    names = zf.namelist()
    folder = "Viaje Portugal 2027"
    assert f"{folder}/Viaje.base" in names
    assert f"{folder}/Ruta/01 Oporto.md" in names and f"{folder}/Ruta/02 Lisboa.md" in names
    assert f"{folder}/Hitos/Casa da Música — Rem Koolhaas.md" in names
    base = zf.read(f"{folder}/Viaje.base").decode("utf-8")
    assert 'file.inFolder("Viaje Portugal 2027")' in base and "type: table" in base and "name: Curados" in base
    note = zf.read(f"{folder}/Hitos/Casa da Música — Rem Koolhaas.md").decode("utf-8")
    assert note.startswith("---\ntipo: \"hito\"\n") and 'estado: "Curado"' in note
    assert 'parada_cercana: "[[01 Oporto]]"' in note and "tiempo_coche_min: 42" in note
    assert "cierra <lunes>" in note


def test_health(client):
    assert client.get("/api/health").get_json() == {"ok": True}


def test_images_step_attaches_pictures_and_fixes_location(client, fake_geo, monkeypatch):
    found = {"wikidata_id": "Q1", "wikipedia_url": "https://es.wikipedia.org/wiki/X", "lat": 41.16, "lon": -8.63,
             "images": [{"kind": "foto", "url": "https://c/x.jpg", "thumb": "https://c/x_t.jpg", "title": "x.jpg",
                         "page_url": "https://c/File:x.jpg", "source": "wikidata"},
                        {"kind": "plano", "url": "https://c/p.png", "thumb": "https://c/p_t.png", "title": "plan.png",
                         "page_url": "https://c/File:plan.png", "source": "commons"}]}
    monkeypatch.setattr(images, "fetch_images", lambda name, city, lat=None, lon=None: found)
    tid = new_trip(client)
    upload(client, f"/api/trips/{tid}/route", make_xlsx(ROUTE_HEADER, ROUTE_ROWS))
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER[:3], [["Edificio desconocido", "Nadie", "Oporto"]]))
    COORDS["Oporto"] = (41.15, -8.61)
    enrich_all(client, tid)
    lm = client.get(f"/api/trips/{tid}").get_json()["landmarks"][0]
    assert lm["images_status"] == "ok" and lm["wikidata_id"] == "Q1" and lm["wikipedia_url"].endswith("/X")
    assert [im["kind"] for im in lm["images"]] == ["foto", "plano"]
    assert lm["geocode_status"] == "exacta" and lm["lat"] == 41.16       # Wikidata fixed the city-level guess
    assert lm["drive_source"] == "osrm"                                   # ...and the drive was recomputed

    # drop one picture, then ask for a fresh search
    r = client.delete(f"/api/landmarks/{lm['id']}/images/{lm['images'][0]['id']}").get_json()
    assert [im["kind"] for im in r["images"]] == ["plano"]
    r = client.post(f"/api/landmarks/{lm['id']}/images/refresh").get_json()
    assert r["images"] == [] and r["images_status"] == "pendiente"
    assert client.get(f"/api/trips/{tid}").get_json()["pending"]["images"] == 1
    enrich_all(client, tid)
    assert len(client.get(f"/api/trips/{tid}").get_json()["landmarks"][0]["images"]) == 2


def test_images_failure_marks_landmark_and_continues(client, fake_geo, monkeypatch):
    monkeypatch.setattr(images, "fetch_images", lambda *a, **k: (_ for _ in ()).throw(KeyError("weird payload")))
    tid = new_trip(client)
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER, [LANDMARK_ROWS[1]]))
    r = enrich_all(client, tid)
    assert r["done"]
    assert client.get(f"/api/trips/{tid}").get_json()["landmarks"][0]["images_status"] == "fallido"


def _png_bytes(w=2400, h=1800):
    from PIL import Image
    img = Image.new("RGB", (w, h), (200, 30, 30))
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def test_manual_images_url_and_upload(client, app, fake_geo):
    tid = new_trip(client)
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER, [LANDMARK_ROWS[1]]))
    lm = client.get(f"/api/trips/{tid}").get_json()["landmarks"][0]

    r = client.post(f"/api/landmarks/{lm['id']}/images", json={"url": "javascript:alert(1)", "kind": "foto"})
    assert r.status_code == 400
    r = client.post(f"/api/landmarks/{lm['id']}/images", json={"url": "https://example.com/serralves.jpg", "kind": "plano"})
    assert r.status_code == 201
    assert r.get_json()["images"][0] == {**r.get_json()["images"][0], "kind": "plano", "source": "manual",
                                         "url": "https://example.com/serralves.jpg", "thumb": "https://example.com/serralves.jpg"}

    r = client.post(f"/api/landmarks/{lm['id']}/images", data={"file": (io.BytesIO(_png_bytes()), "foto.png"), "kind": "foto"},
                    content_type="multipart/form-data")
    assert r.status_code == 201
    up = [im for im in r.get_json()["images"] if im["kind"] == "foto"][0]
    assert up["url"].startswith(f"/uploads/{lm['id']}/") and up["thumb"].endswith("_thumb.jpg") and up["title"] == "foto.png"
    from PIL import Image
    big = client.get(up["url"]); data = big.data; big.close()          # close: Windows locks served files
    assert big.status_code == 200 and data[:2] == bytes([0xFF, 0xD8])   # resized JPEG
    assert max(Image.open(io.BytesIO(data)).size) == 1600
    small = client.get(up["thumb"]); sdata = small.data; small.close()
    assert max(Image.open(io.BytesIO(sdata)).size) == 640
    r = client.post(f"/api/landmarks/{lm['id']}/images", data={"file": (io.BytesIO(b"not an image"), "x.jpg"), "kind": "foto"},
                    content_type="multipart/form-data")
    assert r.status_code == 400

    # the automatic search keeps manual pictures; the standalone copy embeds the uploaded file
    enrich_all(client, tid)
    client.post(f"/api/landmarks/{lm['id']}/images/refresh"); enrich_all(client, tid)
    lm = client.get(f"/api/trips/{tid}").get_json()["landmarks"][0]
    assert sorted(im["source"] for im in lm["images"]) == ["manual", "manual"]
    html = client.get(f"/api/trips/{tid}/export/html").data.decode("utf-8")
    assert "data:image/jpeg;base64," in html and "/uploads/" not in html.split("window.__ARCHTRIP__")[1][:200000]

    # deleting removes the stored files
    import pathlib
    folder = pathlib.Path(app.config["UPLOAD_DIR"]) / str(lm["id"])
    assert len(list(folder.glob("*.jpg"))) == 2
    client.delete(f"/api/landmarks/{lm['id']}/images/{up['id']}")
    assert len(list(folder.glob("*.jpg"))) == 0


def test_links_step_fills_empty_urls_only(client, fake_geo, monkeypatch):
    monkeypatch.setattr(links, "find_archdaily", lambda n, a, c="", k="": "https://www.archdaily.com/1/x")
    monkeypatch.setattr(links, "find_av", lambda n, a, c="": "https://arquitecturaviva.com/obras/x")
    tid = new_trip(client)
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER, LANDMARK_ROWS[:2]))   # Serralves has its own AD url
    assert client.get(f"/api/trips/{tid}").get_json()["pending"]["links"] == 2
    enrich_all(client, tid)
    lms = {l["name"]: l for l in client.get(f"/api/trips/{tid}").get_json()["landmarks"]}
    assert lms["Casa da Música"]["url_archdaily"] == "https://www.archdaily.com/1/x"
    assert lms["Casa da Música"]["url_av"] == "https://arquitecturaviva.com/obras/x"
    assert lms["Museo de Serralves"]["url_archdaily"] == "https://www.archdaily.com/x"      # professor's URL kept
    assert all(l["links_status"] == "ok" for l in lms.values())
    r = client.patch(f"/api/landmarks/{lms['Casa da Música']['id']}", json={"retry_links": True}).get_json()
    assert r["links_status"] == "pendiente"


def test_image_order_and_kind(client, fake_geo):
    tid = new_trip(client)
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER, [LANDMARK_ROWS[1]]))
    lm = client.get(f"/api/trips/{tid}").get_json()["landmarks"][0]
    for i in range(3):
        client.post(f"/api/landmarks/{lm['id']}/images", json={"url": f"https://example.com/{i}.jpg", "kind": "foto"})
    ids = [im["id"] for im in client.get(f"/api/trips/{tid}").get_json()["landmarks"][0]["images"]]
    r = client.put(f"/api/landmarks/{lm['id']}/images/order", json={"ids": ids[::-1]}).get_json()
    assert [im["id"] for im in r["images"]] == ids[::-1]
    r = client.patch(f"/api/landmarks/{lm['id']}/images/{ids[0]}", json={"kind": "plano"}).get_json()
    assert [im["kind"] for im in r["images"]] == ["foto", "foto", "plano"]     # plans sort after photos
    assert client.put(f"/api/landmarks/{lm['id']}/images/order", json={"ids": "x"}).status_code == 400


def test_upload_status_column_never_overrides_manual_curation(client, fake_geo):
    tid = new_trip(client)
    hdr = LANDMARK_HEADER[:3] + ["Estado"]
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(hdr, [["A", "x", "Oporto", "descartado"], ["B", "x", "Oporto", None], ["C", "x", "Oporto", "posible"]]))
    lms = {l["name"]: l for l in client.get(f"/api/trips/{tid}").get_json()["landmarks"]}
    assert (lms["A"]["status"], lms["B"]["status"], lms["C"]["status"]) == ("descartado", "pendiente", "posible")
    client.patch(f"/api/landmarks/{lms['C']['id']}", json={"status": "curado"})          # the professor decides
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(hdr, [["A", "x", "Oporto", "posible"], ["B", "x", "Oporto", "posible"], ["C", "x", "Oporto", "descartado"]]))
    lms = {l["name"]: l for l in client.get(f"/api/trips/{tid}").get_json()["landmarks"]}
    assert lms["A"]["status"] == "descartado"      # already set in the tool: kept
    assert lms["B"]["status"] == "posible"         # was still pending: the sheet may set it
    assert lms["C"]["status"] == "curado"          # manual curation wins


def test_prompt_download(client):
    r = client.get("/api/templates/prompt.md")
    assert r.status_code == 200 and "attachment" not in (r.headers.get("Content-Disposition") or "")
    text = r.data.decode("utf-8")
    assert "plantilla_hitos.xlsx" in text and "Edificio | Arquitecto | Ciudad" in text and "Estado" in text
    assert "[ciudad 1" in text and "[nombre del viaje]" in text and "descartado" in text and "posible" in text
    r = client.get("/api/templates/prompt.md?download=1")
    assert "prompt-archtrip.md" in r.headers["Content-Disposition"]
