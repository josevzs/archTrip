"""Panda recoge las fotos de los alumnos del buzón (archtrip/inbox.py), sin red: un doble del
buzón con los mismos métodos que inbox.Client."""
import io
import pathlib
import sqlite3

import pytest
from PIL import Image

from archtrip import create_app, inbox
from archtrip.db import get_db
from test_api import fake_geo  # noqa: F401  (fixture)
from test_publicar import embedded, seed


def jpeg(color=(200, 30, 30), size=(90, 60)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=90)
    return buf.getvalue()


class FakeInbox:
    """El buzón visto desde panda: envíos pendientes, sus bytes, y lo que panda le dice."""

    def __init__(self):
        self.subs, self.objects = [], {}
        self.collected_ids, self.catalogs, self.catalog_puts = [], {}, 0
        self.fail_fetch, self.fail_pending, self.fail_collected = set(), False, False

    def add(self, sid, landmark_id, data, author="Lucía", mime="image/jpeg", trip="portugal-2027", **extra):
        key = f"{trip}/{sid}.jpg"
        self.objects[key] = data
        self.subs.append(dict({"id": sid, "trip": trip, "landmark_id": landmark_id, "landmark_name": "?",
                               "author": author, "object_key": key, "bytes": len(data), "mime": mime,
                               "taken_at": "2027-04-12T10:00:00", "lat": 41.15, "lon": -8.63,
                               "created_at": "2027-04-12T21:00:00Z"}, **extra))

    def pending(self):
        if self.fail_pending:
            raise inbox.InboxError("El buzón no contesta")
        return [s for s in self.subs if s["id"] not in self.collected_ids]

    def fetch(self, key):
        if key in self.fail_fetch:
            raise inbox.InboxError("se cortó la descarga")
        return self.objects[key]

    def collected(self, ids):
        if self.fail_collected:
            raise inbox.InboxError("El buzón no contesta")
        self.collected_ids.extend(ids)
        return {"done": len(ids)}

    def put_catalog(self, trip, data):
        self.catalog_puts += 1
        self.catalogs[trip] = data
        return {"ok": True}

    def status(self):
        return {"nuevos": len(self.pending()), "bytes": 0}


@pytest.fixture
def box():
    inbox._catalog_sent.clear()
    inbox.health.update(last_ok=None, failing_since=None, last_error=None, last_status=None)
    return FakeInbox()


@pytest.fixture
def trip(app, client):
    """El viaje de test_publicar.seed, publicado como portugal-2027."""
    tid, lms = seed(client)
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO publications (trip_id, project) VALUES (?, 'portugal-2027')", (tid,))
        db.commit()
    return tid, lms


def run(app, box):
    with app.app_context():
        return inbox.run_once(get_db(), box)


def query(app, sql, *args):
    with app.app_context():
        return [dict(r) for r in get_db().execute(sql, args).fetchall()]


def test_a_photo_is_collected_into_the_archive(app, client, box, trip):
    tid, (a, b, c) = trip
    photo = jpeg()
    box.add("s1", a["id"], photo, author="Lucía García")
    assert run(app, box) == ["s1"]
    assert box.collected_ids == ["s1"]                       # solo después de tenerla, se borra allí

    rec = query(app, "SELECT * FROM inbox_photos")[0]
    original = pathlib.Path(rec["original"])
    assert original.parent.name == "fotos-alumnos" and original.name == "s1.jpg"
    assert original.read_bytes() == photo                    # el original, intacto
    assert (rec["state"], rec["author"], rec["landmark_id"], rec["trip_id"], rec["bytes"]) == \
        ("pendiente", "Lucía García", a["id"], tid, len(photo))
    assert rec["taken_at"] == "2027-04-12T10:00:00" and rec["lat"] == 41.15

    im = query(app, "SELECT * FROM landmark_images WHERE id = ?", rec["image_id"])[0]
    assert (im["source"], im["author"], im["submission_id"], im["approved"], im["kind"]) == \
        ("alumno", "Lucía García", "s1", 0, "foto")
    up = pathlib.Path(app.config["UPLOAD_DIR"])
    assert (up / im["url"][len("/uploads/"):]).is_file() and (up / im["thumb"][len("/uploads/"):]).is_file()
    # al final de la lista: una foto sin aprobar no pasa a ser la principal
    positions = [r["position"] for r in query(app, "SELECT position FROM landmark_images WHERE landmark_id = ?", a["id"])]
    assert im["position"] == max(positions)


def test_a_pending_photo_is_not_published_until_approved(app, client, box, trip):
    tid, (a, _, _) = trip
    box.add("s1", a["id"], jpeg())
    run(app, box)
    url = query(app, "SELECT url FROM landmark_images WHERE submission_id = 's1'")[0]["url"]
    html = client.get(f"/api/trips/{tid}/export/html?alumnos=1").data.decode("utf-8")
    assert url[1:] not in html and "s1" not in html.split("window.__ARCHTRIP__")[1]
    # en la app del profesor sí está, para que la vea
    assert any(im.get("submission_id") == "s1" for lm in client.get(f"/api/trips/{tid}").get_json()["landmarks"]
               for im in lm["images"])

    with app.app_context():
        get_db().execute("UPDATE landmark_images SET approved = 1 WHERE submission_id = 's1'")
        get_db().commit()
    data = embedded(client.get(f"/api/trips/{tid}/export/html?alumnos=1").data.decode("utf-8"))
    shown = [im for lm in data["landmarks"] for im in lm["images"] if im["source"] == "alumno"]
    assert shown[0]["url"] == url[1:] and shown[0]["author"] == "Lucía" and "submission_id" not in shown[0]


def test_a_failed_download_is_not_marked_collected(app, client, box, trip):
    _, (a, b, _) = trip
    box.add("s1", a["id"], jpeg())
    box.add("s2", b["id"], jpeg((0, 0, 200)))
    box.fail_fetch.add("portugal-2027/s1.jpg")
    assert run(app, box) == ["s2"]                           # la otra sí se recoge
    assert box.collected_ids == ["s2"]
    assert [r["id"] for r in query(app, "SELECT id FROM inbox_photos")] == ["s2"]
    assert not (pathlib.Path(app.config["DB_PATH"]).parent / "fotos-alumnos" / "s1.jpg").exists()

    box.fail_fetch.clear()                                   # en la siguiente pasada, sí
    assert run(app, box) == ["s1"]
    assert sorted(box.collected_ids) == ["s1", "s2"]


def test_a_photo_that_arrives_cut_is_retried(app, client, box, trip):
    _, (a, _, _) = trip
    box.add("s1", a["id"], jpeg())
    box.subs[0]["bytes"] += 1000                             # el buzón dice que pesaba más
    assert run(app, box) == [] and box.collected_ids == []
    assert query(app, "SELECT COUNT(*) AS n FROM inbox_photos")[0]["n"] == 0


def test_a_lost_collected_notice_does_not_duplicate(app, client, box, trip):
    _, (a, _, _) = trip
    box.add("s1", a["id"], jpeg())
    box.fail_collected = True
    with pytest.raises(inbox.InboxError):
        run(app, box)
    assert box.collected_ids == []                           # el buzón la sigue teniendo…
    box.fail_collected = False
    assert run(app, box) == ["s1"]                           # …y vuelve a ofrecerla
    assert query(app, "SELECT COUNT(*) AS n FROM landmark_images WHERE submission_id = 's1'")[0]["n"] == 1
    assert query(app, "SELECT COUNT(*) AS n FROM inbox_photos")[0]["n"] == 1


def test_no_landmark_or_unreadable_photo_is_kept_and_let_go(app, client, box, trip):
    _, (a, b, _) = trip
    client.delete(f"/api/landmarks/{b['id']}")
    box.add("sin", b["id"], jpeg())
    box.add("rota", a["id"], b"\xff\xd8\xff" + b"no es un jpeg de verdad" * 10)
    assert sorted(run(app, box)) == ["rota", "sin"]          # no se quedan en el cubo para siempre
    recs = {r["id"]: r for r in query(app, "SELECT * FROM inbox_photos")}
    assert recs["sin"]["state"] == "sin_obra" and recs["sin"]["image_id"] is None
    assert recs["rota"]["state"] == "ilegible" and recs["rota"]["image_id"] is None
    assert all(pathlib.Path(r["original"]).is_file() for r in recs.values())   # y el original, guardado
    assert query(app, "SELECT COUNT(*) AS n FROM landmark_images WHERE source = 'alumno'")[0]["n"] == 0


def test_a_landmark_number_from_another_trip_is_not_guessed(app, client, box, trip):
    from test_api import new_trip
    other = new_trip(client, "Otro viaje")
    from conftest import LANDMARK_HEADER, LANDMARK_ROWS, make_xlsx, upload
    upload(client, f"/api/trips/{other}/landmarks", make_xlsx(LANDMARK_HEADER, LANDMARK_ROWS))
    foreign = client.get(f"/api/trips/{other}").get_json()["landmarks"][0]["id"]
    box.add("s1", foreign, jpeg())
    run(app, box)
    assert query(app, "SELECT state FROM inbox_photos")[0]["state"] == "sin_obra"


def test_the_catalog_is_what_students_see_and_is_sent_only_when_it_changes(app, client, box, trip):
    _, (a, b, c) = trip
    run(app, box)
    cat = box.catalogs["portugal-2027"]
    assert cat["trip_name"] == 'Portugal "2027"'
    assert [lm["id"] for lm in cat["landmarks"]] == [a["id"], b["id"]]          # sin el descartado
    assert cat["landmarks"][0]["day"] == 1 and set(cat["landmarks"][0]) == {"id", "name", "architect", "city", "lat", "lon", "day"}
    run(app, box)
    assert box.catalog_puts == 1                                                  # nada ha cambiado
    client.patch(f"/api/landmarks/{c['id']}", json={"status": "curado"})
    run(app, box)
    assert box.catalog_puts == 2 and len(box.catalogs["portugal-2027"]["landmarks"]) == 3


def test_a_private_or_unpublished_trip_has_no_catalog(app, client, box, trip):
    tid, _ = trip
    client.patch(f"/api/trips/{tid}", json={"private": True})
    run(app, box)
    assert box.catalogs == {}


def test_the_inbox_failing_is_remembered_for_the_alarms(app, client, box, trip):
    box.fail_pending = True
    with pytest.raises(inbox.InboxError):
        run(app, box)
    assert inbox.health["failing_since"] is not None and "no contesta" in inbox.health["last_error"]
    box.fail_pending = False
    run(app, box)
    assert inbox.health["failing_since"] is None and inbox.health["last_ok"]
    assert inbox.health["last_status"] == {"nuevos": 0, "bytes": 0}


def test_refreshing_photos_keeps_the_students_ones(app, client, box, trip):
    _, (a, _, _) = trip
    box.add("s1", a["id"], jpeg())
    run(app, box)
    client.post(f"/api/landmarks/{a['id']}/images/refresh")
    assert query(app, "SELECT COUNT(*) AS n FROM landmark_images WHERE submission_id = 's1'")[0]["n"] == 1


def test_a_copied_trip_keeps_the_signature_but_not_the_submission(app, client, box, trip):
    tid, (a, _, _) = trip
    box.add("s1", a["id"], jpeg(), author="Lucía")
    run(app, box)
    r = client.post(f"/api/trips/{tid}/copy", json={})
    assert r.status_code in (200, 201)
    copy = client.get(f"/api/trips/{r.get_json()['id']}").get_json()
    ims = [im for lm in copy["landmarks"] for im in lm["images"] if im["source"] == "alumno"]
    assert [(im["author"], im["approved"], im["submission_id"]) for im in ims] == [("Lucía", 0, None)]


def test_without_settings_the_worker_does_not_start(app, monkeypatch):
    monkeypatch.delenv("ARCHTRIP_INBOX_URL", raising=False)
    assert inbox.configured() is False
    monkeypatch.setenv("ARCHTRIP_INBOX_URL", "https://buzon.example")
    monkeypatch.setenv("ARCHTRIP_INBOX_SECRET", "s")
    assert inbox.configured() is True


def test_the_client_speaks_to_the_inbox_routes(monkeypatch):
    calls = []

    class Resp:
        status_code, text, content = 200, "", b"bytes"

        def json(self):
            return {"items": [], "done": 1, "ok": True}

    def fake(method, url, headers=None, timeout=None, **kw):
        calls.append((method, url, headers, kw))
        return Resp()
    monkeypatch.setattr(inbox.requests, "request", fake)
    c = inbox.Client("https://buzon.example/", "secreto")
    c.pending(); c.fetch("japon-2026/ab.jpg"); c.collected(["a"]); c.put_catalog("japon-2026", {"landmarks": []})
    assert [(m, u) for m, u, _, _ in calls] == [
        ("GET", "https://buzon.example/panda/pendientes"),
        ("GET", "https://buzon.example/panda/objeto/japon-2026/ab.jpg"),
        ("POST", "https://buzon.example/panda/recogido"),
        ("PUT", "https://buzon.example/panda/catalogo/japon-2026")]
    assert all(h["Authorization"] == "Bearer secreto" for _, _, h, _ in calls)
    assert calls[3][2]["Content-Type"] == "application/json"
    assert calls[2][3]["json"] == {"ids": ["a"]}


def test_old_databases_get_the_new_columns_with_photos_approved(tmp_path):
    path = tmp_path / "vieja.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE landmark_images (id INTEGER PRIMARY KEY AUTOINCREMENT, landmark_id INTEGER NOT NULL,
                   kind TEXT NOT NULL, url TEXT NOT NULL, thumb TEXT NOT NULL, title TEXT, page_url TEXT,
                   source TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0)""")
    con.execute("INSERT INTO landmark_images (landmark_id, kind, url, thumb, source) VALUES (1, 'foto', 'u', 't', 'commons')")
    con.commit()
    con.close()
    create_app(db_path=path)
    con = sqlite3.connect(path)
    assert con.execute("SELECT approved, author, submission_id FROM landmark_images").fetchone() == (1, None, None)
