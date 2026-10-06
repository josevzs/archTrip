"""Gestión de las visitas: estado de organización, notas con historial y su PDF.

Es una capa aparte del curado: un hito puede estar *curado* y la visita sin pedir, o *tal vez* y
ya confirmada. Los dos PDF de antes (día a día y con fotos) no deben enterarse de nada de esto."""
import io

from pypdf import PdfReader

from conftest import LANDMARK_HEADER, LANDMARK_ROWS, ROUTE_HEADER, ROUTE_ROWS, make_xlsx, upload
from test_api import enrich_all, fake_geo, new_trip  # noqa: F401  (fixture + helper)


def pdf_text(data):
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)


def seed(client):
    tid = new_trip(client)
    upload(client, f"/api/trips/{tid}/route", make_xlsx(ROUTE_HEADER, ROUTE_ROWS))
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER, LANDMARK_ROWS))
    return tid


def trip(client, tid):
    return client.get(f"/api/trips/{tid}").get_json()


def first(client, tid):
    return trip(client, tid)["landmarks"][0]


def test_the_organisation_state_is_its_own_thing(client, fake_geo):
    tid = seed(client)
    lm = first(client, tid)
    assert lm["org_status"] is None and lm["org_notes"] == []

    r = client.patch(f"/api/landmarks/{lm['id']}", json={"org_status": "esperando"})
    assert r.status_code == 200 and r.get_json()["org_status"] == "esperando"
    # el curado no se toca
    assert r.get_json()["status"] == "pendiente"
    assert client.patch(f"/api/landmarks/{lm['id']}", json={"org_status": "ni idea"}).status_code == 400
    # y se puede quitar
    assert client.patch(f"/api/landmarks/{lm['id']}", json={"org_status": ""}).get_json()["org_status"] is None


def test_each_update_is_kept_with_its_date(client, fake_geo):
    tid = seed(client)
    lm = first(client, tid)
    assert client.post(f"/api/landmarks/{lm['id']}/notas", json={"text": "   "}).status_code == 400

    r = client.post(f"/api/landmarks/{lm['id']}/notas", json={"text": "Llamado, piden correo"})
    assert r.status_code == 201
    client.post(f"/api/landmarks/{lm['id']}/notas", json={"text": "Correo enviado"})
    notes = first(client, tid)["org_notes"]
    # la última primero, y la anterior sigue ahí
    assert [n["text"] for n in notes] == ["Correo enviado", "Llamado, piden correo"]
    assert all(n["at"] and n["landmark_id"] == lm["id"] for n in notes)

    out = client.delete(f"/api/notas/{notes[0]['id']}").get_json()
    assert [n["text"] for n in out["org_notes"]] == ["Llamado, piden correo"]
    assert client.delete(f"/api/notas/{notes[0]['id']}").status_code == 404


def test_the_journal_undoes_a_state_and_a_note(client, fake_geo):
    tid = seed(client)
    lm = first(client, tid)
    client.patch(f"/api/landmarks/{lm['id']}", json={"org_status": "problema"})
    client.post(f"/api/landmarks/{lm['id']}/notas", json={"text": "Cerrado por obras"})

    client.post("/api/admin/login", json={"password": "admin"})
    sid = client.get("/api/admin/sessions").get_json()[0]["id"]
    journal = client.get(f"/api/admin/sessions/{sid}/changes").get_json()
    nota = next(c for c in journal if c["action"] == "org_note")
    estado = next(c for c in journal if c["action"] == "edit" and c["field"] == "org_status")

    assert client.post(f"/api/admin/changes/{nota['id']}/revert").status_code == 200
    assert first(client, tid)["org_notes"] == []
    assert client.post(f"/api/admin/changes/{estado['id']}/revert").status_code == 200
    assert first(client, tid)["org_status"] is None

    # y deshacer el borrado la devuelve
    journal = client.get(f"/api/admin/sessions/{sid}/changes").get_json()
    borrado = next(c for c in journal if c["action"] == "org_note_delete")
    assert client.post(f"/api/admin/changes/{borrado['id']}/revert").status_code == 200
    assert [n["text"] for n in first(client, tid)["org_notes"]] == ["Cerrado por obras"]


def planned(client, tid):
    """Un día con el primer hito dentro, que es lo que imprime el PDF."""
    stop = trip(client, tid)["stops"][0]
    day = client.post(f"/api/trips/{tid}/days", json={"date": "2027-04-12", "stop_id": stop["id"]}).get_json()
    lm = first(client, tid)
    client.post(f"/api/days/{day['id']}/items", json={"kind": "hito", "landmark_id": lm["id"], "at_time": "10:00"})
    return day, lm


def test_the_organisation_pdf_carries_the_state_and_the_history(client, fake_geo):
    tid = seed(client)
    day, lm = planned(client, tid)
    client.patch(f"/api/landmarks/{lm['id']}", json={"org_status": "guia"})
    client.post(f"/api/landmarks/{lm['id']}/notas", json={"text": "Lo lleva el guia local"})
    client.post(f"/api/landmarks/{lm['id']}/notas", json={"text": "Pendiente de fecha"})

    r = client.get(f"/api/trips/{tid}/export/organizacion")
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert "organizacion" in r.headers["Content-Disposition"]
    text = pdf_text(r.data)
    import unicodedata
    plano = "".join(c for c in unicodedata.normalize("NFKD", text.upper()) if not unicodedata.combining(c))
    assert "PARA EL GUIA LOCAL" in plano
    assert "Pendiente de fecha" in text and "Lo lleva el guia local" in text   # historial entero
    assert "Casa da" in text and "10:00" in text                               # el itinerario sigue ahí


def test_a_landmark_with_an_open_matter_but_no_day_still_shows_up(client, fake_geo):
    tid = seed(client)
    planned(client, tid)
    suelto = trip(client, tid)["landmarks"][2]
    client.patch(f"/api/landmarks/{suelto['id']}", json={"org_status": "por_contactar"})
    text = pdf_text(client.get(f"/api/trips/{tid}/export/organizacion").data)
    assert "SIN DIA ASIGNADO" in text.upper().replace("Í", "I")
    assert suelto["name"][:10] in text


def test_the_other_two_pdfs_do_not_change(client, fake_geo):
    tid = seed(client)
    day, lm = planned(client, tid)
    antes = client.get(f"/api/trips/{tid}/export/itinerario").data
    client.patch(f"/api/landmarks/{lm['id']}", json={"org_status": "problema"})
    client.post(f"/api/landmarks/{lm['id']}/notas", json={"text": "Esto no sale en el itinerario"})
    despues = client.get(f"/api/trips/{tid}/export/itinerario").data
    texto = pdf_text(despues)
    assert "PROBLEMA" not in texto.upper() and "no sale en el itinerario" not in texto
    assert len(antes) > 0 and abs(len(antes) - len(despues)) < 200      # el mismo documento
