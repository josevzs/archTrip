"""La copia de consulta para los alumnos (`/export/html?alumnos=1`), la que publica
scripts/publicar.sh: solo lectura y sin nada de la cocina del viaje."""
import json

from conftest import LANDMARK_HEADER, LANDMARK_ROWS, ROUTE_HEADER, ROUTE_ROWS, make_xlsx, upload
from test_api import fake_geo, new_trip  # noqa: F401  (fixture + helper)


def embedded(html):
    start = html.index("window.__ARCHTRIP__ = ") + len("window.__ARCHTRIP__ = ")
    return json.loads(html[start:html.index(";</script>", start)])


def seed(client):
    """Casa da Música curada y con gestión, Serralves posible, el tercero descartado; un día con
    los tres dentro y una nota."""
    tid = new_trip(client, 'Portugal "2027"')
    upload(client, f"/api/trips/{tid}/route", make_xlsx(ROUTE_HEADER, ROUTE_ROWS))
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER, LANDMARK_ROWS))
    a, b, c = client.get(f"/api/trips/{tid}").get_json()["landmarks"]
    client.patch(f"/api/landmarks/{a['id']}", json={"status": "curado", "org_status": "problema"})
    client.post(f"/api/landmarks/{a['id']}/notas", json={"text": "No contestan al correo"})
    client.patch(f"/api/landmarks/{b['id']}", json={"status": "posible"})
    client.patch(f"/api/landmarks/{c['id']}", json={"status": "descartado"})
    day = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12", "notes": "Hotel Infante Sagres"}).get_json()
    for lm in (a, b, c):
        client.post(f"/api/days/{day['id']}/items", json={"landmark_id": lm["id"]})
    client.post(f"/api/days/{day['id']}/items", json={"kind": "nota", "text": "Tren a Lisboa"})
    return tid, (a, b, c)


def test_the_student_copy_leaves_out_the_kitchen(client):
    tid, (a, b, c) = seed(client)
    r = client.get(f"/api/trips/{tid}/export/html?alumnos=1")
    assert r.status_code == 200 and "-alumnos.html" in r.headers["Content-Disposition"]
    html = r.data.decode("utf-8")
    data = embedded(html)
    assert data["readonly"] == {"pdfs": []}
    assert [lm["id"] for lm in data["landmarks"]] == [a["id"], b["id"]]          # sin el descartado
    assert all("org_status" not in lm and "org_notes" not in lm for lm in data["landmarks"])
    assert "No contestan" not in html
    assert "private" not in data["trip"] and "pending" not in data
    items = data["days"][0]["items"]
    assert [it.get("landmark_id") for it in items if it["kind"] == "hito"] == [a["id"], b["id"]]
    assert any(it["kind"] == "nota" and it["text"] == "Tren a Lisboa" for it in items)
    assert data["days"][0]["notes"] == "Hotel Infante Sagres"


def test_a_pending_landmark_goes_out_only_if_it_is_in_a_day(client):
    tid, (a, b, c) = seed(client)
    client.patch(f"/api/landmarks/{b['id']}", json={"status": "pendiente"})
    client.patch(f"/api/landmarks/{c['id']}", json={"status": "pendiente"})
    day = client.get(f"/api/trips/{tid}").get_json()["days"][0]
    item_c = next(it for it in day["items"] if it.get("landmark_id") == c["id"])
    client.delete(f"/api/items/{item_c['id']}")
    data = embedded(client.get(f"/api/trips/{tid}/export/html?alumnos=1").data.decode("utf-8"))
    assert [lm["id"] for lm in data["landmarks"]] == [a["id"], b["id"]]


def test_the_student_copy_has_a_title_a_preview_and_no_indexing(client):
    tid, _ = seed(client)
    html = client.get(f"/api/trips/{tid}/export/html?alumnos=1&pdfs=1").data.decode("utf-8")
    head = html.split("</head>")[0]
    assert "<title>Portugal &quot;2027&quot;</title>" in head
    assert '<meta name="robots" content="noindex, nofollow">' in head
    assert '<meta property="og:title" content="Portugal &quot;2027&quot;">' in head
    assert "Oporto → Lisboa" in head and "2 obras de arquitectura" in head
    assert [f["file"] for f in embedded(html)["readonly"]["pdfs"]] == ["itinerario.pdf", "itinerario-fotos.pdf"]


def test_the_normal_copy_does_not_change(client):
    tid, _ = seed(client)
    html = client.get(f"/api/trips/{tid}/export/html").data.decode("utf-8")
    data = embedded(html)
    assert "readonly" not in data and len(data["landmarks"]) == 3
    assert data["landmarks"][0]["org_status"] == "problema"
    assert "<title>archTrip</title>" in html and "noindex" not in html


def test_the_published_pdf_leaves_out_the_same(client):
    tid, (a, b, c) = seed(client)
    from test_itinerary import pdf_text
    full = pdf_text(client.get(f"/api/trips/{tid}/export/itinerario").data)
    students = pdf_text(client.get(f"/api/trips/{tid}/export/itinerario?alumnos=1").data)
    assert c["name"] in full and c["name"] not in students
    assert a["name"] in students and "Tren a Lisboa" in students
