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
    r = rows[0]["items"][0]
    assert (r["time"], r["line"]) == ("09:30", "REM KOOLHAAS — Casa da Música")
    assert "Oporto" in r["meta"] and "2005" in r["meta"] and "min en coche desde Oporto" in r["meta"]
    assert r["status"] == "pendiente" and r["confirm"] is False


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


def test_status_and_pending_confirmation_show_up_in_the_pdf(client, fake_geo):
    tid = seed(client)
    day = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12"}).get_json()
    fijo, opcional, _ = trip(client, tid)["landmarks"]
    client.patch(f"/api/landmarks/{fijo['id']}", json={"status": "curado"})
    client.patch(f"/api/landmarks/{opcional['id']}", json={"status": "posible"})
    it = client.post(f"/api/days/{day['id']}/items", json={"landmark_id": fijo["id"], "at_time": "09:30"}).get_json()["items"][0]
    client.post(f"/api/days/{day['id']}/items", json={"landmark_id": opcional["id"]})

    r = client.patch(f"/api/items/{it['id']}", json={"needs_confirm": 1})
    assert r.get_json()["items"][0]["needs_confirm"] == 1
    assert trip(client, tid)["days"][0]["items"][0]["needs_confirm"] == 1

    text = pdf_text(client.get(f"/api/trips/{tid}/export/itinerario").data)
    assert "FIJO" in text and "OPCIONAL" in text and "PENDIENTE DE CONFIRMAR" in text
    assert "■ fijo · ■ opcional · ■ pendiente de confirmar" in text    # la leyenda de arriba

    # la marca se quita igual que se pone, y queda en el diario
    client.patch(f"/api/items/{it['id']}", json={"needs_confirm": 0})
    assert "PENDIENTE DE CONFIRMAR" not in pdf_text(client.get(f"/api/trips/{tid}/export/itinerario").data)
    client.post("/api/admin/login", json={"password": "admin"})
    sid = client.get("/api/admin/sessions").get_json()[0]["id"]
    marks = [c for c in client.get(f"/api/admin/sessions/{sid}/changes").get_json() if c["field"] == "needs_confirm"]
    assert [(c["old_value"], c["new_value"]) for c in marks] == [("0", "1"), ("1", "0")]
    assert client.post(f"/api/admin/changes/{marks[1]['id']}/revert").get_json()["result"] == "revertido"
    assert trip(client, tid)["days"][0]["items"][0]["needs_confirm"] == 1


def test_gallery_pdf_carries_the_photos(client, fake_geo, monkeypatch):
    import io as _io
    from PIL import Image as PILImage
    from archtrip import export

    buf = _io.BytesIO()
    noise = PILImage.effect_noise((240, 180), 60).convert("RGB")     # que no comprima a nada
    noise.save(buf, "JPEG")
    shot = buf.getvalue()
    asked = []

    def fake_photo(url, cache):
        asked.append(url)
        return shot if url and "rota" not in url else None       # una falla a propósito

    monkeypatch.setattr(export, "photo_bytes", fake_photo)

    tid = seed(client)
    day = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12"}).get_json()
    a, b, _ = trip(client, tid)["landmarks"]
    client.patch(f"/api/landmarks/{a['id']}", json={"url_image1": "https://example.com/foto.jpg"})
    client.patch(f"/api/landmarks/{b['id']}", json={"url_image1": "https://example.com/rota.jpg"})
    client.post(f"/api/days/{day['id']}/items", json={"landmark_id": a["id"], "at_time": "09:30"})
    client.post(f"/api/days/{day['id']}/items", json={"landmark_id": b["id"]})

    plain = client.get(f"/api/trips/{tid}/export/itinerario")
    gallery = client.get(f"/api/trips/{tid}/export/itinerario?fotos=1")
    assert gallery.status_code == 200 and gallery.mimetype == "application/pdf"
    assert "itinerario-fotos-portugal-2027.pdf" in gallery.headers["Content-Disposition"]
    assert len(gallery.data) > len(plain.data) + 1000          # la foto va dentro
    assert len(PdfReader(io.BytesIO(gallery.data)).pages[0].images) == 1      # la rota no, claro
    assert not PdfReader(io.BytesIO(plain.data)).pages[0].images
    assert "https://example.com/foto.jpg" in asked
    # la que falla no rompe el documento: sigue teniendo el texto de los dos hitos
    text = pdf_text(gallery.data)
    assert "Casa da Música" in text and "Museo de Serralves" in text


def test_photos_are_prefetched_one_by_one_and_cached_on_disk(client, fake_geo, monkeypatch, app):
    """El frontend baja las fotos de una en una para poder enseñar una barra de progreso;
    quedan en disco, así que el PDF siguiente sale sin volver a salir a internet."""
    import io as _io
    from PIL import Image as PILImage
    from archtrip import export

    buf = _io.BytesIO()
    PILImage.new("RGB", (200, 150), (70, 70, 70)).save(buf, "JPEG")

    class Resp:
        status_code = 200
        content = buf.getvalue()

    hits = []

    def fake_get(url, **kw):
        hits.append(url)
        if "rota" in url:
            raise RuntimeError("se cayó la descarga")
        return Resp()

    monkeypatch.setattr(export.requests, "get", fake_get)

    tid = seed(client)
    day = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12"}).get_json()
    a, b, c = trip(client, tid)["landmarks"]
    client.patch(f"/api/landmarks/{a['id']}", json={"url_image1": "https://example.com/a.jpg"})
    client.patch(f"/api/landmarks/{b['id']}", json={"url_image1": "https://example.com/rota.jpg"})
    for lm in (a, b, c):
        client.post(f"/api/days/{day['id']}/items", json={"landmark_id": lm["id"]})

    steps = []
    for _ in range(10):
        r = client.post(f"/api/trips/{tid}/export/itinerario/fotos/next").get_json()
        steps.append((r["total"], r["remaining"]))
        if r["done"]:
            break
    assert steps == [(2, 1), (2, 0)]              # dos fotos, una por llamada
    assert len(hits) == 2 and sorted(hits) == ["https://example.com/a.jpg", "https://example.com/rota.jpg"]

    # ya está todo en caché: ni una descarga más, ni siquiera la que falló
    again = client.post(f"/api/trips/{tid}/export/itinerario/fotos/next").get_json()
    assert again == {"done": True, "remaining": 0, "total": 2, "url": None}
    assert len(hits) == 2
    with app.app_context():
        files = list((__import__("pathlib").Path(app.config["DB_PATH"]).parent / "cache" / "photos").glob("*.img"))
    assert len(files) == 2 and sum(1 for f in files if f.stat().st_size == 0) == 1   # la rota, vacía

    def boom(url, **kw):
        raise AssertionError("no debería volver a internet: " + url)

    monkeypatch.setattr(export.requests, "get", boom)
    gallery = client.get(f"/api/trips/{tid}/export/itinerario?fotos=1")
    assert gallery.status_code == 200 and len(gallery.data) > len(client.get(f"/api/trips/{tid}/export/itinerario").data)


def test_gallery_photos_share_the_same_width_without_stretching():
    """Las verticales quedan más altas, no más estrechas; solo se estrecha lo que no cabría."""
    from archtrip.export import photo_size

    apaisada = photo_size(1200, 800, 34, 60)
    vertical = photo_size(800, 1200, 34, 60)
    assert apaisada[0] == vertical[0] == 34                    # mismo ancho
    assert round(apaisada[1], 2) == 22.67 and round(vertical[1], 2) == 51.0
    assert round(apaisada[0] / apaisada[1], 3) == round(1200 / 800, 3)     # sin deformar
    assert round(vertical[0] / vertical[1], 3) == round(800 / 1200, 3)
    muy_alta = photo_size(400, 1600, 34, 60)                   # esta sí se estrecha, tope de alto
    assert muy_alta[1] == 60 and muy_alta[0] < 34
    assert photo_size(0, 0, 34, 60) is None
