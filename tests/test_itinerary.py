"""Days of the trip: dates, landmarks and notes in order, and the printable itinerary."""
import io
import zipfile

from pypdf import PdfReader

from conftest import LANDMARK_HEADER, LANDMARK_ROWS, ROUTE_HEADER, ROUTE_ROWS, make_xlsx, upload
from test_api import enrich_all, fake_geo, new_trip  # noqa: F401  (fixture + helper)


def pdf_text(data):
    """Todo el texto del PDF, página a página."""
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)


def seed(client):
    tid = new_trip(client)
    upload(client, f"/api/trips/{tid}/route", make_xlsx(ROUTE_HEADER, ROUTE_ROWS))
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER, LANDMARK_ROWS))
    return tid


def trip(client, tid):
    return client.get(f"/api/trips/{tid}").get_json()


def test_days_carry_dates_and_a_base_city(client):
    tid = seed(client)
    stop = trip(client, tid)["stops"][0]
    r = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12", "stop_id": stop["id"], "title": "Llegada"})
    assert r.status_code == 201
    day = r.get_json()
    assert (day["date"], day["title"], day["stop_city"], day["items"]) == ("2027-04-12", "Llegada", "Oporto", [])
    assert client.post(f"/api/trips/{tid}/days", json={"date": "12/4/2027"}).status_code == 400
    assert client.post(f"/api/trips/{tid}/days", json={"stop_id": 9999}).status_code == 400

    # undated days go last; the rest sort by date whatever the order they were created in
    client.post(f"/api/trips/{tid}/days", json={})
    client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-10"})
    assert [d["date"] for d in trip(client, tid)["days"]] == ["2027-04-10", "2027-04-12", None]

    r = client.patch(f"/api/days/{day['id']}", json={"date": "2027-04-13", "notes": "hotel junto a la estación"})
    assert (r.get_json()["date"], r.get_json()["notes"]) == ("2027-04-13", "hotel junto a la estación")
    assert client.delete(f"/api/days/{day['id']}").status_code == 204
    assert [d["date"] for d in trip(client, tid)["days"]] == ["2027-04-10", None]


def test_items_are_landmarks_or_notes_kept_in_order(client):
    tid = seed(client)
    day = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12"}).get_json()
    a, b, c = trip(client, tid)["landmarks"]
    client.post(f"/api/days/{day['id']}/items", json={"landmark_id": a["id"], "at_time": "09:30"})
    client.post(f"/api/days/{day['id']}/items", json={"kind": "nota", "text": "comida por la Ribeira"})
    r = client.post(f"/api/days/{day['id']}/items", json={"landmark_id": b["id"], "at_time": "16:00"})
    items = r.get_json()["items"]
    assert [(i["kind"], i["at_time"]) for i in items] == [("hito", "09:30"), ("nota", None), ("hito", "16:00")]
    assert items[1]["text"] == "comida por la Ribeira"

    assert client.post(f"/api/days/{day['id']}/items", json={"kind": "nota", "text": "  "}).status_code == 400
    assert client.post(f"/api/days/{day['id']}/items", json={"landmark_id": 9999}).status_code == 400
    assert client.post(f"/api/days/{day['id']}/items", json={"landmark_id": a["id"], "at_time": "25:00"}).status_code == 400

    # reorder, retime, rewrite
    ids = [items[2]["id"], items[0]["id"], items[1]["id"]]
    assert [i["id"] for i in client.put(f"/api/days/{day['id']}/items/order", json={"ids": ids}).get_json()["items"]] == ids
    client.patch(f"/api/items/{items[0]['id']}", json={"at_time": "10:15"})
    client.patch(f"/api/items/{items[1]['id']}", json={"text": "comida en Matosinhos"})
    fresh = trip(client, tid)["days"][0]["items"]
    assert [(i["id"], i["at_time"]) for i in fresh][:2] == [(items[2]["id"], "16:00"), (items[0]["id"], "10:15")]
    assert fresh[2]["text"] == "comida en Matosinhos"

    # the same landmark may appear twice (two visits), and removing an item keeps the landmark
    client.post(f"/api/days/{day['id']}/items", json={"landmark_id": a["id"]})
    assert len(trip(client, tid)["days"][0]["items"]) == 4
    client.delete(f"/api/items/{items[0]['id']}")
    data = trip(client, tid)
    assert len(data["days"][0]["items"]) == 3 and len(data["landmarks"]) == 3
    # deleting the landmark drops its lines from the itinerary
    client.delete(f"/api/landmarks/{a['id']}")
    assert [i["kind"] for i in trip(client, tid)["days"][0]["items"]] == ["hito", "nota"]
    assert c["id"]


def test_items_move_between_days(client):
    tid = seed(client)
    d1 = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12"}).get_json()
    d2 = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-13"}).get_json()
    lm = trip(client, tid)["landmarks"][0]
    item = client.post(f"/api/days/{d1['id']}/items", json={"landmark_id": lm["id"]}).get_json()["items"][0]
    client.post(f"/api/days/{d2['id']}/items", json={"kind": "nota", "text": "tren"})
    r = client.patch(f"/api/items/{item['id']}", json={"day_id": d2["id"]})
    assert r.get_json()["id"] == d2["id"] and [i["kind"] for i in r.get_json()["items"]] == ["nota", "hito"]
    days = trip(client, tid)["days"]
    assert days[0]["items"] == [] and len(days[1]["items"]) == 2


def test_itinerary_export_pdf_and_obsidian(client, fake_geo):
    tid = seed(client)
    stop = trip(client, tid)["stops"][0]
    day = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12", "stop_id": stop["id"],
                                                      "title": "Oporto a pie", "notes": "recoger llaves"}).get_json()
    lm = trip(client, tid)["landmarks"][0]
    client.post(f"/api/days/{day['id']}/items", json={"landmark_id": lm["id"], "at_time": "09:30"})
    client.post(f"/api/days/{day['id']}/items", json={"kind": "nota", "text": "comida por la Ribeira"})
    client.post(f"/api/trips/{tid}/days", json={})

    r = client.get(f"/api/trips/{tid}/export/itinerario")
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert "itinerario-portugal-2027.pdf" in r.headers["Content-Disposition"]
    assert r.data[:4] == b"%PDF" and len(r.data) > 2000

    text = pdf_text(r.data)
    assert "DÍA 1 · LUNES 12 DE ABRIL DE 2027 · OPORTO A PIE" in text
    assert "Base: Oporto" in text and "recoger llaves" in text
    assert "09:30" in text and "REM KOOLHAAS — Casa da Música" in text
    assert "comida por la Ribeira" in text and "sin nada planificado todavía" in text
    assert "Generado con el sistema archTrip el " in text and "página 1 de " in text
    # el PDF se reparte: no lleva la barra de créditos de la aplicación
    for fuera in ("Ko-fi", "ko-fi", "GitHub", "Vargas", "2026 ·"):
        assert fuera not in text, fuera

    zf = zipfile.ZipFile(__import__("io").BytesIO(client.get(f"/api/trips/{tid}/export/obsidian").data))
    note = zf.read("Portugal 2027/Itinerario.md").decode("utf-8")
    assert "## Día 1 · lunes 12 de abril de 2027 · Oporto a pie" in note
    assert "**09:30** [[Casa da Música — Rem Koolhaas|Casa da Música]]" in note
    assert "*comida por la Ribeira*" in note


def test_itinerary_rows_are_what_gets_printed(client, fake_geo, app):
    tid = seed(client)
    day = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12"}).get_json()
    lm = trip(client, tid)["landmarks"][0]
    client.post(f"/api/days/{day['id']}/items", json={"landmark_id": lm["id"], "at_time": "09:30"})
    enrich_all(client, tid)
    from archtrip import export
    with app.test_request_context():
        from archtrip.db import get_db, row
        from archtrip.routes import _trip_payload
        payload = _trip_payload(get_db(), row(get_db().execute("SELECT * FROM trips WHERE id = ?", (tid,))))
    rows = export.itinerary_rows(payload)
    assert rows[0]["head"] == "Día 1 · lunes 12 de abril de 2027"
    time, line, meta = rows[0]["items"][0]
    assert (time, line) == ("09:30", "REM KOOLHAAS — Casa da Música")
    assert "Oporto" in meta and "2005" in meta and "min en coche desde Oporto" in meta


def test_itinerary_travels_in_the_standalone_copy(client):
    tid = seed(client)
    day = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12"}).get_json()
    lm = trip(client, tid)["landmarks"][0]
    client.post(f"/api/days/{day['id']}/items", json={"landmark_id": lm["id"], "at_time": "09:30"})
    html = client.get(f"/api/trips/{tid}/export/html").data.decode("utf-8")
    assert '"days":' in html and '"at_time": "09:30"'.replace(" ", "") in html.replace(" ", "")


def test_itinerary_changes_are_journaled_and_revertible(client):
    tid = seed(client)
    day = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12"}).get_json()
    a, b, _ = trip(client, tid)["landmarks"]
    item = client.post(f"/api/days/{day['id']}/items", json={"landmark_id": a["id"], "at_time": "09:30"}).get_json()["items"][0]
    client.post(f"/api/days/{day['id']}/items", json={"landmark_id": b["id"]})
    client.patch(f"/api/items/{item['id']}", json={"at_time": "11:00"})
    client.patch(f"/api/days/{day['id']}", json={"title": "Oporto"})
    client.delete(f"/api/items/{item['id']}")

    client.post("/api/admin/login", json={"password": "admin"})
    sid = client.get("/api/admin/sessions").get_json()[0]["id"]
    journal = client.get(f"/api/admin/sessions/{sid}/changes").get_json()
    changes = {c["action"]: c for c in journal}     # la última de cada tipo
    assert {"day_create", "item_add", "item_edit", "day_edit", "item_delete"} <= set(changes)
    assert changes["item_edit"]["old_value"] == "09:30"
    assert [c["landmark_name"] for c in journal if c["action"] == "item_add"] == ["Casa da Música", "Museo de Serralves"]

    assert client.post(f"/api/admin/changes/{changes['item_delete']['id']}/revert").get_json()["result"] == "revertido"
    items = trip(client, tid)["days"][0]["items"]
    assert [i["at_time"] for i in items if i["id"] == item["id"]] == ["11:00"]
    client.post(f"/api/admin/changes/{changes['item_edit']['id']}/revert")
    client.post(f"/api/admin/changes/{changes['day_edit']['id']}/revert")
    day_now = trip(client, tid)["days"][0]
    assert day_now["title"] is None
    assert [i["at_time"] for i in day_now["items"] if i["id"] == item["id"]] == ["09:30"]

    # undoing the rest (the two items and the day itself) leaves no itinerary at all
    for c in [c for c in journal if c["action"] == "item_add"] + [changes["day_create"]]:
        assert client.post(f"/api/admin/changes/{c['id']}/revert").get_json()["result"] in ("revertido", "día recuperado")
    assert trip(client, tid)["days"] == []
    assert len(trip(client, tid)["landmarks"]) == 3       # los hitos siguen ahí


def test_revert_day_delete_brings_back_its_items(client):
    tid = seed(client)
    day = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12", "title": "Oporto"}).get_json()
    lm = trip(client, tid)["landmarks"][0]
    client.post(f"/api/days/{day['id']}/items", json={"landmark_id": lm["id"], "at_time": "09:30"})
    client.post(f"/api/days/{day['id']}/items", json={"kind": "nota", "text": "cena"})
    client.delete(f"/api/days/{day['id']}")
    assert trip(client, tid)["days"] == []

    client.post("/api/admin/login", json={"password": "admin"})
    sid = client.get("/api/admin/sessions").get_json()[0]["id"]
    ch = [c for c in client.get(f"/api/admin/sessions/{sid}/changes").get_json() if c["action"] == "day_delete"][0]
    assert client.post(f"/api/admin/changes/{ch['id']}/revert").get_json()["result"] == "día recuperado"
    back = trip(client, tid)["days"][0]
    assert (back["date"], back["title"]) == ("2027-04-12", "Oporto")
    assert [(i["kind"], i["at_time"], i["text"]) for i in back["items"]] == [("hito", "09:30", None), ("nota", None, "cena")]


def test_trip_delete_and_restore_keeps_the_itinerary(client):
    tid = seed(client)
    day = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12"}).get_json()
    lm = trip(client, tid)["landmarks"][0]
    client.post(f"/api/days/{day['id']}/items", json={"landmark_id": lm["id"], "at_time": "09:30"})
    client.delete(f"/api/trips/{tid}")
    client.post("/api/admin/login", json={"password": "admin"})
    sid = client.get("/api/admin/sessions").get_json()[0]["id"]
    ch = [c for c in client.get(f"/api/admin/sessions/{sid}/changes").get_json() if c["action"] == "trip_delete"][0]
    client.post(f"/api/admin/changes/{ch['id']}/revert")
    days = trip(client, tid)["days"]
    assert len(days) == 1 and [i["at_time"] for i in days[0]["items"]] == ["09:30"]
