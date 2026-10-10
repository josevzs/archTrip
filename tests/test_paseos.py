"""Paseos: hitos que son un recorrido en vez de un punto.

Lo que hay que sostener: que un paseo es **un hito más** (se cura, entra en un día, sale en los
PDF) y que lo único propio suyo —la geometría y que su punto es la entrada— se guarda y se
devuelve intacto."""
import io
import json

from pypdf import PdfReader

from archtrip import walks
from conftest import LANDMARK_HEADER, LANDMARK_ROWS, ROUTE_HEADER, ROUTE_ROWS, make_xlsx, upload
from test_api import enrich_all, fake_geo, new_trip  # noqa: F401  (fixture + helper)

GION = {
    "type": "Feature",
    "geometry": {"type": "LineString", "coordinates": [[135.7750, 35.0040], [135.7760, 35.0045],
                                                       [135.7772, 35.0050]]},
    "properties": {"walk_id": "kyoto-gion", "name": "Gion · Shirakawa", "city": "Kioto",
                   "theme": "Frentes urbanos y casas de té", "status": "posible",
                   "notes": "B · se anda en una tarde", "attribution": "© OpenStreetMap",
                   "access_lat": 35.0038, "access_lon": 135.7748},
}
UJI = {
    "type": "Feature",
    "geometry": {"type": "MultiLineString", "coordinates": [
        [[135.8060, 34.8940], [135.8070, 34.8945]],
        [[135.8080, 34.8950], [135.8090, 34.8955]]]},
    "properties": {"name": "Uji · calles del té", "city": "Uji", "theme": "Calles comerciales",
                   "length_m": 3748.8},
}


def coleccion(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def subir_paseos(client, tid, data, filename="paseos.geojson"):
    return client.post(f"/api/trips/{tid}/paseos",
                       data={"file": (io.BytesIO(json.dumps(data).encode("utf-8")), filename)},
                       content_type="multipart/form-data")


def seed(client):
    tid = new_trip(client)
    upload(client, f"/api/trips/{tid}/route", make_xlsx(ROUTE_HEADER, ROUTE_ROWS))
    return tid


def trip(client, tid):
    return client.get(f"/api/trips/{tid}").get_json()


# ------------------------------------------------------------------ el formato

def test_a_walk_is_read_with_its_entrance_and_its_length():
    paseos, errores = walks.parse(coleccion(GION, UJI))
    assert errores == []
    gion, uji = paseos
    # la entrada la manda el archivo; el tema ocupa el sitio del arquitecto
    assert (gion["lat"], gion["lon"]) == (35.0038, 135.7748)
    assert gion["architect"] == "Frentes urbanos y casas de té" and gion["city"] == "Kioto"
    assert gion["status"] == "posible" and "se anda en una tarde" in gion["notes"]
    assert "attribution" in gion["notes"]                      # de dónde sale el trazado
    assert gion["length_m"] > 200                               # se mide sola
    # sin access_*, la entrada es el primer punto del primer tramo
    assert (uji["lat"], uji["lon"]) == (34.8940, 135.8060)
    assert uji["length_m"] == 3748.8                            # la que diga el archivo


def test_what_is_not_a_walk_is_reported_not_swallowed():
    punto = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [135.0, 35.0]},
             "properties": {"name": "Un edificio"}}
    sin_nombre = {"type": "Feature", "geometry": GION["geometry"], "properties": {}}
    paseos, errores = walks.parse(coleccion(punto, sin_nombre, GION))
    assert [p["name"] for p in paseos] == ["Gion · Shirakawa"]
    assert any("no es un recorrido" in e and "Point" in e for e in errores)
    assert any("sin nombre" in e for e in errores)
    assert walks.parse({"type": "Topology"})[1] == ["El archivo no es un GeoJSON con recorridos (se esperaba FeatureCollection)"]


def test_a_geometry_on_its_own_also_works():
    paseos, errores = walks.parse(GION["geometry"])
    assert errores == [] or paseos == []          # sin nombre no entra, pero no revienta
    paseos, _ = walks.parse({"type": "Feature", "geometry": GION["geometry"], "properties": {"name": "X"}})
    assert paseos[0]["name"] == "X" and paseos[0]["geometry"]["type"] == "LineString"


# -------------------------------------------------------------------- la subida

def test_uploading_walks_creates_landmarks_that_behave_like_any_other(client, fake_geo):
    tid = seed(client)
    r = subir_paseos(client, tid, coleccion(GION, UJI))
    assert r.status_code == 200 and r.get_json()["added"] == 2

    lms = trip(client, tid)["landmarks"]
    gion = next(l for l in lms if l["name"].startswith("Gion"))
    assert gion["kind"] == "paseo" and gion["status"] == "posible"
    assert gion["geocode_status"] == "manual" and (gion["lat"], gion["lon"]) == (35.0038, 135.7748)
    assert json.loads(gion["geometry"])["type"] == "LineString"
    assert gion["length_m"] > 200
    assert gion["links_status"] == "ok"          # un paseo no tiene ficha en ArchDaily ni en AV
    assert gion["images"] == []                  # pero puede llevar foto, como cualquier hito

    # se cura, se edita y se mete en un día igual que un edificio
    assert client.patch(f"/api/landmarks/{gion['id']}", json={"status": "curado"}).get_json()["status"] == "curado"
    day = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12"}).get_json()
    item = client.post(f"/api/days/{day['id']}/items", json={"kind": "hito", "landmark_id": gion["id"]})
    assert item.status_code == 201


def test_uploading_the_same_file_again_updates_instead_of_duplicating(client, fake_geo):
    tid = seed(client)
    subir_paseos(client, tid, coleccion(GION))
    client.patch(f"/api/landmarks/{trip(client, tid)['landmarks'][0]['id']}", json={"status": "curado"})

    movido = json.loads(json.dumps(GION))
    movido["geometry"]["coordinates"].append([135.7790, 35.0060])
    r = subir_paseos(client, tid, coleccion(movido))
    assert r.get_json() == {"added": 0, "updated": 1, "errors": [], "pending": r.get_json()["pending"]}
    lms = trip(client, tid)["landmarks"]
    assert len(lms) == 1
    assert len(json.loads(lms[0]["geometry"])["coordinates"]) == 4   # el recorrido nuevo
    assert lms[0]["status"] == "curado"                              # el curado no se pierde


def test_bad_uploads_say_what_is_wrong(client, fake_geo):
    tid = seed(client)
    r = client.post(f"/api/trips/{tid}/paseos", data={"file": (io.BytesIO(b"{no es json"), "p.geojson")},
                    content_type="multipart/form-data")
    assert r.status_code == 400 and "GeoJSON" in r.get_json()["error"]
    r = client.post(f"/api/trips/{tid}/paseos", data={"file": (io.BytesIO(b"{}"), "p.xlsx")},
                    content_type="multipart/form-data")
    assert r.status_code == 400 and ".geojson" in r.get_json()["error"]
    assert subir_paseos(client, tid, coleccion()).status_code == 400
    assert client.post("/api/trips/999/paseos").status_code == 404


# ----------------------------------------------------- desde la propia plantilla

def test_a_walk_can_come_in_the_landmarks_template(client, fake_geo):
    tid = seed(client)
    header = LANDMARK_HEADER + ["Tipo", "Recorrido"]
    fila = ["Paseo de Shirakawa", "Casas de té", "Kioto"] + [None] * (len(LANDMARK_HEADER) - 3)
    fila += ["paseo", json.dumps(GION["geometry"])]
    r = upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(header, [fila]))
    assert r.get_json()["walks"] == 1
    lm = trip(client, tid)["landmarks"][0]
    assert lm["kind"] == "paseo" and lm["name"] == "Paseo de Shirakawa"
    assert json.loads(lm["geometry"])["type"] == "LineString"
    # la plantilla manda sobre el archivo: nombre y «arquitecto» son los de la fila
    assert lm["architect"] == "Casas de té"

    # marcado como paseo pero sin recorrido: entra como hito y se avisa
    fila2 = ["Otro sitio", "Alguien", "Kioto"] + [None] * (len(LANDMARK_HEADER) - 3) + ["paseo", None]
    r2 = upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(header, [fila2]))
    assert any("sin recorrido" in e for e in r2.get_json()["errors"])
    assert next(l for l in trip(client, tid)["landmarks"] if l["name"] == "Otro sitio")["kind"] == "hito"


# --------------------------------------------------------------- en los papeles

def test_the_walk_says_how_long_it_is_in_the_pdf(client, fake_geo):
    tid = seed(client)
    subir_paseos(client, tid, coleccion(UJI))
    lm = trip(client, tid)["landmarks"][0]
    day = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12"}).get_json()
    client.post(f"/api/days/{day['id']}/items", json={"kind": "hito", "landmark_id": lm["id"]})
    texto = "\n".join(p.extract_text() for p in
                      PdfReader(io.BytesIO(client.get(f"/api/trips/{tid}/export/itinerario").data)).pages)
    assert "paseo" in texto and "3,7 km" in texto


def test_the_route_photo_comes_from_the_template(client, fake_geo):
    tid = new_trip(client)
    foto = "https://example.com/kioto.jpg"
    upload(client, f"/api/trips/{tid}/route",
           make_xlsx(["Orden", "Ciudad", "País", "Foto (URL)", "Notas"],
                     [[1, "Kioto", "Japón", foto, None], [2, "Uji", "Japón", None, None]]))
    kioto, uji = trip(client, tid)["stops"]
    assert kioto["photo_url"] == foto and kioto["photo_thumb"] == foto
    assert kioto["images_status"] == "ok"        # con foto puesta no se busca ninguna
    assert uji["photo_url"] is None and uji["images_status"] == "pendiente"
