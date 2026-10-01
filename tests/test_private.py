"""Variantes de un viaje y viajes que solo existen dentro de casa.

La app se ve a la vez en la red local (por su nombre interno) y fuera por el túnel de Tailscale,
que entra con el nombre `*.ts.net`. Un viaje privado no debe asomar por ahí ni de refilón."""
from conftest import LANDMARK_HEADER, LANDMARK_ROWS, ROUTE_HEADER, ROUTE_ROWS, make_xlsx, upload
from test_api import enrich_all, fake_geo, new_trip  # noqa: F401  (fixture + helpers)

FUERA = {"Host": "panda-server.tail3a4288.ts.net"}      # como llega una petición por el túnel
CASA = {"Host": "archtrip.panda.internal"}


def seed(client):
    tid = new_trip(client)
    upload(client, f"/api/trips/{tid}/route", make_xlsx(ROUTE_HEADER, ROUTE_ROWS))
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER, LANDMARK_ROWS))
    return tid


def test_a_private_trip_does_not_exist_from_outside(client, fake_geo):
    tid = seed(client)
    enrich_all(client, tid)
    assert client.patch(f"/api/trips/{tid}", json={"private": True}).get_json()["private"] == 1

    assert client.get("/api/trips").get_json() != []                     # en casa se ve
    assert client.get("/api/trips", headers=FUERA).get_json() == []      # fuera no está
    assert client.get("/api/trips", headers=CASA).get_json() != []

    for url in (f"/api/trips/{tid}", f"/api/trips/{tid}/export/html", f"/api/trips/{tid}/export/obsidian",
                f"/api/trips/{tid}/export/itinerario"):
        assert client.get(url, headers=FUERA).status_code == 404, url
        assert client.get(url, headers=CASA).status_code == 200, url
    # tampoco se puede tocar a ciegas desde fuera
    assert client.post(f"/api/trips/{tid}/enrich/next", headers=FUERA).status_code == 404
    assert client.patch(f"/api/trips/{tid}", json={"name": "otro"}, headers=FUERA).status_code == 404
    assert client.delete(f"/api/trips/{tid}", headers=FUERA).status_code == 404
    assert client.post(f"/api/trips/{tid}/days", json={}, headers=FUERA).status_code == 404

    # publicarlo es un solo cambio, y se puede volver a esconder
    client.patch(f"/api/trips/{tid}", json={"private": False})
    assert [t["id"] for t in client.get("/api/trips", headers=FUERA).get_json()] == [tid]
    assert client.get(f"/api/trips/{tid}", headers=FUERA).status_code == 200
    client.patch(f"/api/trips/{tid}", json={"private": True})
    assert client.get("/api/trips", headers=FUERA).get_json() == []


def test_the_rest_of_the_trips_are_untouched_from_outside(client, fake_geo):
    publico, privado = seed(client), seed(client)
    client.patch(f"/api/trips/{privado}", json={"private": True})
    fuera = [t["id"] for t in client.get("/api/trips", headers=FUERA).get_json()]
    assert fuera == [publico] and privado not in fuera
    assert client.get(f"/api/trips/{publico}", headers=FUERA).status_code == 200


def test_health_says_where_the_request_came_from(client):
    assert client.get("/api/health", headers=CASA).get_json()["public"] is False
    assert client.get("/api/health", headers=FUERA).get_json()["public"] is True


def test_the_admin_page_is_not_reachable_from_outside(client):
    seed(client)
    assert client.post("/api/admin/login", json={"password": "admin"}, headers=FUERA).status_code == 401
    assert client.post("/api/admin/login", json={"password": "admin"}, headers=CASA).status_code == 200
    # con la sesión abierta en casa, la cookie no vale desde fuera
    assert client.get("/api/admin/sessions", headers=CASA).status_code == 200
    assert client.get("/api/admin/sessions", headers=FUERA).status_code == 401
    assert client.get("/api/admin/backups", headers=FUERA).status_code == 401


def test_copying_a_trip_leaves_the_original_alone(client, fake_geo):
    tid = seed(client)
    enrich_all(client, tid)
    original = client.get(f"/api/trips/{tid}").get_json()
    lm = original["landmarks"][0]
    client.patch(f"/api/landmarks/{lm['id']}", json={"status": "curado", "notes": "la quiero fija"})
    client.post(f"/api/landmarks/{lm['id']}/images", json={"url": "https://example.com/mia.jpg", "kind": "foto"})
    day = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12", "title": "Oporto",
                                                      "stop_id": original["stops"][0]["id"]}).get_json()
    it = client.post(f"/api/days/{day['id']}/items", json={"landmark_id": lm["id"], "at_time": "09:30"}).get_json()["items"][0]
    client.patch(f"/api/items/{it['id']}", json={"needs_confirm": 1})
    client.post(f"/api/days/{day['id']}/items", json={"kind": "nota", "text": "comida por la Ribeira"})

    r = client.post(f"/api/trips/{tid}/copy", json={"name": "Portugal — variante norte", "private": True})
    assert r.status_code == 201
    copia = r.get_json()
    assert (copia["name"], copia["private"], copia["copied_from"]) == ("Portugal — variante norte", 1, tid)
    assert (copia["landmarks"], copia["stops"], copia["days"]) == (3, 2, 1)

    nueva = client.get(f"/api/trips/{copia['id']}").get_json()
    vieja = client.get(f"/api/trips/{tid}").get_json()
    # mismo contenido...
    assert [l["name"] for l in nueva["landmarks"]] == [l["name"] for l in vieja["landmarks"]]
    assert [s["city"] for s in nueva["stops"]] == [s["city"] for s in vieja["stops"]]
    copiado = [l for l in nueva["landmarks"] if l["name"] == lm["name"]][0]
    assert copiado["status"] == "curado" and copiado["notes"] == "la quiero fija"
    assert [i["url"] for i in copiado["images"]] == [i["url"] for i in
                                                     [x for x in vieja["landmarks"] if x["name"] == lm["name"]][0]["images"]]
    assert copiado["drive_minutes"] == [x for x in vieja["landmarks"] if x["name"] == lm["name"]][0]["drive_minutes"]
    assert nueva["days"][0]["stop_city"] == "Oporto" and nueva["days"][0]["title"] == "Oporto"
    assert [(i["kind"], i["at_time"], i["needs_confirm"]) for i in nueva["days"][0]["items"]] \
        == [("hito", "09:30", 1), ("nota", None, 0)]
    # ...pero son dos viajes distintos: el itinerario de la copia apunta a SUS hitos
    assert nueva["days"][0]["items"][0]["landmark_id"] == copiado["id"] != lm["id"]
    assert {l["id"] for l in nueva["landmarks"]}.isdisjoint({l["id"] for l in vieja["landmarks"]})

    # tocar la copia no mueve el original
    client.patch(f"/api/landmarks/{copiado['id']}", json={"status": "descartado"})
    client.delete(f"/api/days/{nueva['days'][0]['id']}")
    vieja = client.get(f"/api/trips/{tid}").get_json()
    assert [x for x in vieja["landmarks"] if x["name"] == lm["name"]][0]["status"] == "curado"
    assert len(vieja["days"]) == 1 and len(vieja["days"][0]["items"]) == 2
    # y la copia, privada, no se ve desde fuera mientras no se publique
    assert [t["id"] for t in client.get("/api/trips", headers=FUERA).get_json()] == [tid]


def test_copy_keeps_the_original_name_by_default_and_is_public_unless_asked(client, fake_geo):
    tid = seed(client)
    copia = client.post(f"/api/trips/{tid}/copy", json={}).get_json()
    assert copia["name"] == "Portugal 2027 (copia)" and copia["private"] == 0
    assert len(client.get("/api/trips", headers=FUERA).get_json()) == 2
