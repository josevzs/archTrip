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


NOTA = ("A/B [E] · Shinjuku. Tipologías de arquitectura mascota.\n"
        "★ S++ (tu lista final): Para ti, el número uno del viaje\n"
        "▸ Precurado v2: descartado — obra secundaria para este grupo")


def test_the_student_copy_drops_the_curation_marks_but_keeps_the_text(client):
    """El ranking propio, la lista final y el precurado son cocina; la frase que va detrás, no."""
    tid, (a, b, c) = seed(client)
    client.patch(f"/api/landmarks/{a['id']}", json={"notes": NOTA})
    client.patch(f"/api/landmarks/{b['id']}", json={"notes": "▸ Añadido 2026-09-28. Acceso parcial"})
    data = embedded(client.get(f"/api/trips/{tid}/export/html?alumnos=1").data.decode("utf-8"))
    primero, segundo = data["landmarks"]
    assert primero["notes"] == ("Shinjuku. Tipologías de arquitectura mascota.\n"
                                "Para ti, el número uno del viaje")
    assert segundo["notes"] == "Acceso parcial"
    assert "Precurado" not in json.dumps(data, ensure_ascii=False)
    # y en la base de datos sigue entera
    assert client.get(f"/api/trips/{tid}").get_json()["landmarks"][0]["notes"] == NOTA


def test_a_grade_is_not_eaten_from_a_normal_note(client):
    from archtrip.export import public_notes
    assert public_notes("Casa de fin de semana, Gunma") == "Casa de fin de semana, Gunma"
    assert public_notes("A 30 min en coche de Nasushiobara") == "A 30 min en coche de Nasushiobara"
    assert public_notes("B") == ""
    assert public_notes("A+ si permiten visita [R] · Catorce villas") == "Catorce villas"
    assert public_notes("source_quality: draft\nattribution: © OpenStreetMap") == "attribution: © OpenStreetMap"


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


# ------------------------------------------------------------ publicar desde la administración

import os

import pytest  # noqa: E402

from archtrip import publish  # noqa: E402
from archtrip.db import get_db  # noqa: E402


@pytest.fixture
def cloudflare(monkeypatch):
    """Cloudflare de mentira: apunta lo que se crea, se sube y se borra."""
    log = {"created": [], "deployed": [], "deleted": []}
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "cuenta")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "token")
    monkeypatch.setattr(publish, "cloudflare_create",
                        lambda p: log["created"].append(p) or f"https://{p}-x7.pages.dev")
    monkeypatch.setattr(publish, "cloudflare_delete", lambda p: log["deleted"].append(p))
    monkeypatch.setattr(publish, "deploy", lambda p, files: log["deployed"].append((p, sorted(files))))
    return log


def login(client):
    assert client.post("/api/admin/login", json={"password": "admin"}).status_code == 200


def run(app):
    with app.app_context():
        return publish.run_once(get_db())


def item(client, tid):
    return next(i for i in client.get("/api/admin/publicaciones").get_json()["items"] if i["trip_id"] == tid)


def test_publishing_needs_the_admin(client):
    tid, _ = seed(client)
    assert client.get("/api/admin/publicaciones").status_code == 401
    assert client.post(f"/api/admin/publicaciones/{tid}").status_code == 401


def test_publish_from_admin_then_only_when_it_changes(app, client, cloudflare):
    tid, (a, _, _) = seed(client)
    login(client)
    out = client.get("/api/admin/publicaciones").get_json()
    assert out["configured"] is True and item(client, tid)["suggested"] == "portugal-2027"
    assert client.post(f"/api/admin/publicaciones/{tid}", json={"project": "Mal nombre!"}).status_code == 400

    assert client.post(f"/api/admin/publicaciones/{tid}", json={"project": "portugal-2027"}).status_code == 200
    assert run(app) == 1
    assert cloudflare["created"] == ["portugal-2027"]
    assert cloudflare["deployed"] == [("portugal-2027", ["_headers", "index.html", "itinerario-fotos.pdf", "itinerario.pdf"])]
    it = item(client, tid)
    assert it["state"] == "ok" and it["url"] == "https://portugal-2027-x7.pages.dev" and it["published_at"]

    assert run(app) == 0                                        # nada ha cambiado
    client.patch(f"/api/landmarks/{a['id']}", json={"org_status": "confirmado"})
    assert run(app) == 0                                        # la gestión no sale: tampoco cuenta
    client.patch(f"/api/landmarks/{a['id']}", json={"notes": "Abre a las 10"})
    assert run(app) == 1 and cloudflare["created"] == ["portugal-2027"]   # el proyecto ya existía

    client.patch(f"/api/admin/publicaciones/{tid}", json={"auto": False})
    client.patch(f"/api/landmarks/{a['id']}", json={"notes": "Abre a las 11"})
    assert run(app) == 0                                        # sin «actualizar sola», quieto
    client.post(f"/api/admin/publicaciones/{tid}")              # … salvo que se pida a mano
    assert run(app) == 1


def test_private_trips_and_missing_keys(app, client, cloudflare, monkeypatch):
    tid, _ = seed(client)
    login(client)
    client.patch(f"/api/trips/{tid}", json={"private": True})
    r = client.post(f"/api/admin/publicaciones/{tid}")
    assert r.status_code == 400 and "solo en local" in r.get_json()["error"]

    client.patch(f"/api/trips/{tid}", json={"private": False})
    client.post(f"/api/admin/publicaciones/{tid}")
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN")
    run(app)
    it = item(client, tid)
    assert it["state"] == "error" and "CLOUDFLARE_API_TOKEN" in it["error"] and not cloudflare["deployed"]

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "token")
    client.post(f"/api/admin/publicaciones/{tid}")
    run(app)
    client.patch(f"/api/trips/{tid}", json={"private": True})  # ya publicado y luego escondido
    client.patch(f"/api/landmarks/{seed_first(client, tid)}", json={"notes": "otra"})
    run(app)
    assert item(client, tid)["state"] == "error" and len(cloudflare["deployed"]) == 1


def seed_first(client, tid):
    return client.get(f"/api/trips/{tid}").get_json()["landmarks"][0]["id"]


def test_a_failed_upload_is_shown_and_withdraw_deletes_the_site(app, client, cloudflare, monkeypatch):
    tid, _ = seed(client)
    login(client)
    client.post(f"/api/admin/publicaciones/{tid}")

    def broken(project, files):
        raise publish.PublishError("Falló la subida: Authentication error")
    monkeypatch.setattr(publish, "deploy", broken)
    run(app)
    it = item(client, tid)
    assert it["state"] == "error" and "Authentication" in it["error"]

    r = client.delete(f"/api/admin/publicaciones/{tid}")
    assert r.status_code == 200 and cloudflare["deleted"] == ["portugal-2027"]
    assert item(client, tid)["project"] is None
    assert client.delete(f"/api/admin/publicaciones/{tid}").status_code == 400


def test_the_project_name_comes_from_the_trip():
    assert publish.project_name("Tohoku (variante) — 2026") == "tohoku-variante-2026"
    assert publish.project_name("¿?") == "viaje"


@pytest.mark.skipif(os.name == "nt", reason="el wrangler de mentira es un script #!/bin/sh: solo corre en el servidor (Linux), que es donde se publica")
def test_deploy_calls_wrangler_with_the_files(tmp_path, monkeypatch):
    fake = tmp_path / "wrangler"
    seen = tmp_path / "seen.txt"
    fake.write_text(f'#!/bin/sh\necho "$@" > {seen}\nls site >> {seen}\n')
    fake.chmod(0o755)
    monkeypatch.setenv("ARCHTRIP_WRANGLER", str(fake))
    publish.deploy("tohoku", {"index.html": b"<p>hola</p>", "_headers": b"/*\n"})
    args, *listed = seen.read_text().splitlines()
    assert args == "pages deploy site --project-name tohoku --branch main --commit-dirty=true"
    assert listed == ["_headers", "index.html"]

    fake.write_text("#!/bin/sh\necho 'Authentication error [code: 10000]' >&2\nexit 1\n")
    with pytest.raises(publish.PublishError, match="Authentication error"):
        publish.deploy("tohoku", {"index.html": b""})


def test_cloudflare_create_keeps_the_address_it_is_given(monkeypatch):
    calls = []

    class Resp:
        def __init__(self, status, data):
            self.status_code, self._data = status, data

        def json(self):
            return self._data

    def fake(method, url, **kw):
        calls.append((method, url.split("/accounts/")[1], kw.get("json")))
        if method == "POST":       # el nombre ya es nuestro: crear falla y se lee el existente
            return Resp(409, {"success": False, "errors": [{"message": "already exists"}]})
        return Resp(200, {"success": True, "result": {"subdomain": "tohoku-2b4.pages.dev"}})
    monkeypatch.setattr(publish.requests, "request", fake)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc")
    assert publish.cloudflare_create("tohoku") == "https://tohoku-2b4.pages.dev"
    assert calls[0] == ("POST", "acc/pages/projects", {"name": "tohoku", "production_branch": "main"})
    assert calls[1][:2] == ("GET", "acc/pages/projects/tohoku")
