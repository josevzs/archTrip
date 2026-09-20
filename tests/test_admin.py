"""Editing sessions, the change journal and the hidden admin API (undo, backups)."""
import io
import time

from archtrip import audit

from conftest import LANDMARK_HEADER, LANDMARK_ROWS, ROUTE_HEADER, ROUTE_ROWS, make_xlsx, upload
from test_api import fake_geo, new_trip  # noqa: F401  (fixture + helper)


def login(client, password="admin"):
    return client.post("/api/admin/login", json={"password": password})


def landmarks(client, tid):
    return client.get(f"/api/trips/{tid}").get_json()["landmarks"]


def seed(client):
    tid = new_trip(client)
    upload(client, f"/api/trips/{tid}/route", make_xlsx(ROUTE_HEADER, ROUTE_ROWS))
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER, LANDMARK_ROWS))
    return tid


def test_admin_requires_password(client):
    assert client.get("/api/admin/sessions").status_code == 401
    assert client.get("/api/admin/me").get_json() == {"admin": False}
    assert login(client, "nope").status_code == 401
    assert login(client).status_code == 200
    assert client.get("/api/admin/me").get_json() == {"admin": True}
    assert client.get("/api/admin/sessions").status_code == 200
    client.post("/api/admin/logout")
    assert client.get("/api/admin/sessions").status_code == 401


def test_viewing_opens_no_session_but_editing_does(client):
    tid = seed(client)                      # uploads are changes: one session already
    login(client)
    sessions = client.get("/api/admin/sessions").get_json()
    assert len(sessions) == 1 and sessions[0]["open"] and sessions[0]["changes"] == 1 + 1 + 3   # trip, route, 3 landmarks
    client.get(f"/api/trips/{tid}")
    assert len(client.get("/api/admin/sessions").get_json()) == 1

    lm = landmarks(client, tid)[0]
    client.patch(f"/api/landmarks/{lm['id']}", json={"status": "curado"})
    client.patch(f"/api/landmarks/{lm['id']}", json={"status": "curado"})       # no-op: not journaled
    s = client.get("/api/admin/sessions").get_json()[0]
    assert s["changes"] == 6 and s["actions"]["status"] == 1 and s["trips"] == ["Portugal 2027"]

    changes = client.get(f"/api/admin/sessions/{s['id']}/changes").get_json()
    st = [c for c in changes if c["action"] == "status"][0]
    assert (st["landmark_name"], st["old_value"], st["new_value"]) == ("Casa da Música", "pendiente", "curado")


def test_session_closes_after_gap_and_can_be_named(client, app, monkeypatch):
    tid = seed(client)
    lm = landmarks(client, tid)[0]
    monkeypatch.setattr(audit, "SESSION_GAP", 0.05)
    time.sleep(0.1)
    client.patch(f"/api/landmarks/{lm['id']}", json={"status": "posible"})    # same browser, new session
    login(client)
    sessions = client.get("/api/admin/sessions").get_json()
    assert [s["changes"] for s in sessions] == [1, 5]
    assert sessions[0]["ip"] and not sessions[1]["open"]
    r = client.patch(f"/api/admin/sessions/{sessions[1]['id']}", json={"name": "Carga inicial"})
    assert r.get_json()["name"] == "Carga inicial"
    assert client.get("/api/admin/sessions").get_json()[1]["name"] == "Carga inicial"


def test_revert_single_changes(client, fake_geo):
    tid = seed(client)
    a, b = landmarks(client, tid)[:2]
    client.patch(f"/api/landmarks/{a['id']}", json={"status": "descartado", "notes": "fuera", "architect": "OMA"})
    client.patch(f"/api/landmarks/{b['id']}", json={"lat": "41.0", "lon": "-8.0"})
    login(client)
    hist = client.get("/api/admin/changes", query_string={"landmark": "Casa da"}).get_json()
    assert {c["action"] for c in hist} == {"create_landmark", "status", "edit"}
    by_field = {c["field"]: c for c in hist if c["action"] in ("status", "edit")}
    assert by_field["architect"]["old_value"] == "Rem Koolhaas"

    for c in (by_field["status"], by_field["notes"], by_field["architect"]):
        assert client.post(f"/api/admin/changes/{c['id']}/revert").get_json()["result"] == "revertido"
    a2 = landmarks(client, tid)[0]
    assert (a2["status"], a2["notes"], a2["architect"]) == ("pendiente", None, "Rem Koolhaas")
    # the undo is itself journaled and the original is marked
    hist = client.get("/api/admin/changes", query_string={"landmark": str(a["id"])}).get_json()
    assert hist[0]["revert_of"] == by_field["architect"]["id"] and hist[0]["new_value"] == "Rem Koolhaas"
    assert [c for c in hist if c["id"] == by_field["status"]["id"]][0]["reverted_by"]
    assert client.post(f"/api/admin/changes/{by_field['status']['id']}/revert").get_json()["result"] == "ya revertido"

    coords = [c for c in client.get("/api/admin/changes", query_string={"landmark": "Serralves"}).get_json()
              if c["field"] == "coords"][0]
    assert coords["old_value"].startswith("41.15960, -8.65970") and coords["new_value"].endswith("(manual)")
    client.post(f"/api/admin/changes/{coords['id']}/revert")
    b2 = landmarks(client, tid)[1]
    assert (b2["lat"], b2["lon"], b2["geocode_status"], b2["drive_source"]) == (41.1596, -8.6597, "manual", None)


def test_revert_deletions_and_images(client):
    tid = seed(client)
    a = landmarks(client, tid)[0]
    r = client.post(f"/api/landmarks/{a['id']}/images", json={"url": "https://example.com/a.jpg", "kind": "plano"})
    img = r.get_json()["images"][0]
    client.patch(f"/api/landmarks/{a['id']}/images/{img['id']}", json={"kind": "foto"})
    client.delete(f"/api/landmarks/{a['id']}/images/{img['id']}")
    client.delete(f"/api/landmarks/{a['id']}")
    assert len(landmarks(client, tid)) == 2

    login(client)
    sid = client.get("/api/admin/sessions").get_json()[0]["id"]
    changes = {c["action"]: c for c in client.get(f"/api/admin/sessions/{sid}/changes").get_json()}
    assert changes["image_kind"]["old_value"] == "plano" and changes["image_delete"]["old_value"] == "a.jpg"
    assert changes["delete_landmark"]["has_snapshot"]

    assert client.post(f"/api/admin/changes/{changes['delete_landmark']['id']}/revert").get_json()["result"].startswith("hito recuperado")
    back = [lm for lm in landmarks(client, tid) if lm["name"] == "Casa da Música"][0]
    assert back["id"] == a["id"] and back["images"] == []
    client.post(f"/api/admin/changes/{changes['image_delete']['id']}/revert")
    client.post(f"/api/admin/changes/{changes['image_kind']['id']}/revert")
    back = [lm for lm in landmarks(client, tid) if lm["name"] == "Casa da Música"][0]
    assert [(im["url"], im["kind"]) for im in back["images"]] == [("https://example.com/a.jpg", "plano")]


def test_revert_whole_session_in_order(client, app, monkeypatch):
    tid = seed(client)
    monkeypatch.setattr(audit, "SESSION_GAP", 0.05)
    time.sleep(0.1)
    # a "professor" session: curates two, deletes one, renames the trip, clears nothing
    a, b, c = landmarks(client, tid)
    client.patch(f"/api/landmarks/{a['id']}", json={"status": "curado"})
    client.patch(f"/api/landmarks/{a['id']}", json={"status": "posible"})
    client.patch(f"/api/landmarks/{b['id']}", json={"status": "descartado"})
    client.delete(f"/api/landmarks/{c['id']}")
    client.patch(f"/api/trips/{tid}", json={"name": "Portugal (borrador)"})
    login(client)
    sessions = client.get("/api/admin/sessions").get_json()
    prof = sessions[0]
    assert prof["changes"] == 5 and prof["pending"] == 5
    r = client.post(f"/api/admin/sessions/{prof['id']}/revert").get_json()
    assert r["reverted"] == 5
    data = client.get(f"/api/trips/{tid}").get_json()
    assert data["trip"]["name"] == "Portugal 2027"
    assert sorted((lm["name"], lm["status"]) for lm in data["landmarks"]) == sorted(
        (lm["name"], "pendiente") for lm in (a, b, c))
    assert [s for s in client.get("/api/admin/sessions").get_json() if s["id"] == prof["id"]][0]["pending"] == 0
    # reverting again does nothing
    assert client.post(f"/api/admin/sessions/{prof['id']}/revert").get_json()["reverted"] == 0


def test_revert_upload_route_and_clear(client, fake_geo):
    tid = seed(client)
    upload(client, f"/api/trips/{tid}/route", make_xlsx(ROUTE_HEADER, [[1, "Lisboa", "Portugal", None]]))
    assert [s["city"] for s in client.get(f"/api/trips/{tid}").get_json()["stops"]] == ["Lisboa"]
    client.delete(f"/api/trips/{tid}/landmarks")
    assert landmarks(client, tid) == []
    login(client)
    sid = client.get("/api/admin/sessions").get_json()[0]["id"]
    changes = client.get(f"/api/admin/sessions/{sid}/changes").get_json()
    clear = [c for c in changes if c["action"] == "clear_landmarks"][0]
    route = [c for c in changes if c["action"] == "route_upload"][-1]
    assert clear["old_value"] == "3 hitos" and route["new_value"] == "1 paradas"
    assert client.post(f"/api/admin/changes/{clear['id']}/revert").get_json()["result"] == "3 hitos recuperados"
    assert len(landmarks(client, tid)) == 3
    client.post(f"/api/admin/changes/{route['id']}/revert")
    assert [s["city"] for s in client.get(f"/api/trips/{tid}").get_json()["stops"]] == ["Oporto", "Lisboa"]


def test_revert_trip_delete(client):
    tid = seed(client)
    client.delete(f"/api/trips/{tid}")
    assert client.get(f"/api/trips/{tid}").status_code == 404
    login(client)
    sid = client.get("/api/admin/sessions").get_json()[0]["id"]
    ch = [c for c in client.get(f"/api/admin/sessions/{sid}/changes").get_json() if c["action"] == "trip_delete"][0]
    assert client.post(f"/api/admin/changes/{ch['id']}/revert").get_json()["result"].startswith("viaje recuperado")
    data = client.get(f"/api/trips/{tid}").get_json()
    assert len(data["landmarks"]) == 3 and len(data["stops"]) == 2


def test_reupload_changes_are_journaled_and_revertible(client):
    tid = seed(client)
    rows = [list(r) for r in LANDMARK_ROWS]
    rows[0][4] = 2006                       # year changed in the sheet
    upload(client, f"/api/trips/{tid}/landmarks", make_xlsx(LANDMARK_HEADER, rows))
    assert landmarks(client, tid)[0]["year"] == "2006"
    login(client)
    ch = client.get("/api/admin/changes", query_string={"landmark": "Casa da"}).get_json()[0]
    assert ch["action"] == "upload_update"
    client.post(f"/api/admin/changes/{ch['id']}/revert")
    assert landmarks(client, tid)[0]["year"] == "2005"


def test_backups_create_list_download_restore(client, app):
    tid = seed(client)
    login(client)
    r = client.post("/api/admin/backups")
    assert r.status_code == 201
    name = r.get_json()["created"]
    files = client.get("/api/admin/backups").get_json()
    assert any(f["name"] == name for f in files) and any(f["name"].endswith(f"viaje{tid}.html") for f in files)
    assert client.get(f"/api/admin/backups/{name}").status_code == 200
    assert client.get("/api/admin/backups/../secret.key").status_code in (400, 404)
    dump = client.get("/api/admin/export.db")
    assert dump.status_code == 200 and b"CREATE TABLE" in dump.data

    a = landmarks(client, tid)[0]
    client.patch(f"/api/landmarks/{a['id']}", json={"status": "curado"})
    r = client.post(f"/api/admin/backups/{name}/restore").get_json()
    assert r["restored"] == name and r["saved_before"].startswith("pre-restore-")
    assert landmarks(client, tid)[0]["status"] == "pendiente"
    # the state before the restore was kept, and the restore itself is in the journal
    assert any(f["name"] == r["saved_before"] for f in client.get("/api/admin/backups").get_json())
    sid = client.get("/api/admin/sessions").get_json()[0]["id"]
    last = client.get(f"/api/admin/sessions/{sid}/changes").get_json()[-1]
    assert last["action"] == "restore" and not last["revertible"]
