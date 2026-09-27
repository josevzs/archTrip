import io
import re

from flask import Blueprint, abort, jsonify, request, send_file

from . import audit, enrich, excel, export, prompt, uploads
from .db import get_db, row, rows

api = Blueprint("api", __name__)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
STATUSES = ("pendiente", "curado", "posible", "descartado")
EDITABLE = ("name", "architect", "city", "address", "year", "notes",
            "url_archdaily", "url_av", "url_image1", "url_image2")

LANDMARK_SELECT = """
SELECT l.*, s.city AS nearest_stop_city, s.position AS nearest_stop_position
FROM landmarks l LEFT JOIN route_stops s ON s.id = l.nearest_stop_id
"""


def _trip_or_404(db, trip_id):
    t = row(db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)))
    if t is None:
        abort(404, description="Viaje no encontrado")
    return t


def _attach_images(db, landmarks):
    if not landmarks:
        return landmarks
    by_id = {lm["id"]: lm for lm in landmarks}
    for lm in landmarks:
        lm["images"] = []
    marks = ",".join("?" * len(by_id))
    for im in rows(db.execute(
            f"SELECT * FROM landmark_images WHERE landmark_id IN ({marks}) "
            "ORDER BY landmark_id, kind, position, id",
            tuple(by_id))):
        by_id[im["landmark_id"]]["images"].append(im)
    return landmarks


def _landmark_or_404(db, lm_id):
    lm = row(db.execute(LANDMARK_SELECT + " WHERE l.id = ?", (lm_id,)))
    if lm is None:
        abort(404, description="Hito no encontrado")
    return _attach_images(db, [lm])[0]


def _trip_days(db, trip_id):
    """Days in date order (undated last), each with its ordered items."""
    days = rows(db.execute(
        "SELECT d.*, s.city AS stop_city FROM trip_days d LEFT JOIN route_stops s ON s.id = d.stop_id "
        "WHERE d.trip_id = ? ORDER BY (d.date IS NULL OR d.date = ''), d.date, d.position, d.id", (trip_id,)))
    if days:
        by_id = {d["id"]: dict(d, items=[]) for d in days}
        marks = ",".join("?" * len(by_id))
        for it in rows(db.execute(f"SELECT * FROM day_items WHERE day_id IN ({marks}) ORDER BY day_id, position, id",
                                  tuple(by_id))):
            by_id[it["day_id"]]["items"].append(it)
        days = [by_id[d["id"]] for d in days]
    return days


def _trip_payload(db, trip):
    stops = rows(db.execute("SELECT * FROM route_stops WHERE trip_id = ? ORDER BY position",
                            (trip["id"],)))
    landmarks = _attach_images(db, rows(db.execute(
        LANDMARK_SELECT + " WHERE l.trip_id = ? ORDER BY l.sort_order, l.id", (trip["id"],))))
    return {"trip": trip, "stops": stops, "landmarks": landmarks, "days": _trip_days(db, trip["id"]),
            "pending": enrich.pending_counts(db, trip["id"])}


def _uploaded_file():
    f = request.files.get("file")
    if f is None or not f.filename:
        abort(400, description="No se ha enviado ningún archivo")
    if not f.filename.lower().endswith(".xlsx"):
        abort(400, description="El archivo debe ser un Excel (.xlsx)")
    return io.BytesIO(f.read())


@api.errorhandler(400)
@api.errorhandler(404)
def _json_error(err):
    return jsonify({"error": err.description}), err.code


@api.get("/health")
def health():
    get_db().execute("SELECT 1")
    return jsonify({"ok": True})


# ------------------------------------------------------------------ trips

@api.get("/trips")
def list_trips():
    db = get_db()
    return jsonify(rows(db.execute("""
        SELECT t.*,
               (SELECT COUNT(*) FROM landmarks l WHERE l.trip_id = t.id) AS landmark_count,
               (SELECT COUNT(*) FROM landmarks l WHERE l.trip_id = t.id AND l.status = 'curado') AS curated_count,
               (SELECT COUNT(*) FROM route_stops s WHERE s.trip_id = t.id) AS stop_count
        FROM trips t ORDER BY t.created_at DESC, t.id DESC
    """)))


@api.post("/trips")
def create_trip():
    name = (request.get_json(silent=True) or {}).get("name", "").strip()
    if not name:
        abort(400, description="El viaje necesita un nombre")
    db = get_db()
    cur = db.execute("INSERT INTO trips (name) VALUES (?)", (name,))
    audit.log(db, "trip_create", cur.lastrowid, new=name)
    db.commit()
    return jsonify(row(db.execute("SELECT * FROM trips WHERE id = ?", (cur.lastrowid,)))), 201


@api.get("/trips/<int:trip_id>")
def get_trip(trip_id):
    db = get_db()
    return jsonify(_trip_payload(db, _trip_or_404(db, trip_id)))


@api.patch("/trips/<int:trip_id>")
def rename_trip(trip_id):
    db = get_db()
    trip = _trip_or_404(db, trip_id)
    name = (request.get_json(silent=True) or {}).get("name", "").strip()
    if not name:
        abort(400, description="El viaje necesita un nombre")
    if name != trip["name"]:
        db.execute("UPDATE trips SET name = ? WHERE id = ?", (name, trip_id))
        audit.log(db, "trip_rename", trip_id, old=trip["name"], new=name)
        db.commit()
    return jsonify(row(db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,))))


@api.delete("/trips/<int:trip_id>")
def delete_trip(trip_id):
    db = get_db()
    trip = _trip_or_404(db, trip_id)
    audit.log(db, "trip_delete", trip_id, old=trip["name"], snapshot=audit.trip_snapshot(db, trip_id))
    db.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
    db.commit()
    return "", 204


# -------------------------------------------------------------- templates

@api.get("/templates/ruta.xlsx")
def template_route():
    return send_file(io.BytesIO(excel.route_template()), mimetype=XLSX,
                     as_attachment=True, download_name="plantilla_ruta.xlsx")


@api.get("/templates/hitos.xlsx")
def template_landmarks():
    return send_file(io.BytesIO(excel.landmarks_template()), mimetype=XLSX,
                     as_attachment=True, download_name="plantilla_hitos.xlsx")


@api.get("/templates/prompt.md")
def template_prompt():
    """Instructions for an AI assistant to produce the two Excel files for any trip."""
    text = prompt.build()
    return send_file(io.BytesIO(text.encode("utf-8")), mimetype="text/markdown",
                     as_attachment=("download" in request.args), download_name="prompt-archtrip.md")


# ---------------------------------------------------------------- uploads

@api.post("/trips/<int:trip_id>/route")
def upload_route(trip_id):
    db = get_db()
    _trip_or_404(db, trip_id)
    stops, errors = excel.parse_route(_uploaded_file())
    if not stops:
        return jsonify({"error": "No se ha podido leer ninguna parada", "errors": errors}), 400
    audit.log(db, "route_upload", trip_id, new=f"{len(stops)} paradas",
              snapshot=rows(db.execute("SELECT * FROM route_stops WHERE trip_id = ? ORDER BY position", (trip_id,))))
    db.execute("DELETE FROM route_stops WHERE trip_id = ?", (trip_id,))
    db.executemany(
        "INSERT INTO route_stops (trip_id, position, city, country, notes) VALUES (?, ?, ?, ?, ?)",
        [(trip_id, s["position"], s["city"], s["country"], s["notes"]) for s in stops],
    )
    # the route changed, so every drive time must be recomputed
    db.execute("UPDATE landmarks SET nearest_stop_id = NULL, drive_minutes = NULL, drive_km = NULL, "
               "drive_source = NULL WHERE trip_id = ?", (trip_id,))
    db.commit()
    return jsonify({"added": len(stops), "errors": errors})


@api.post("/trips/<int:trip_id>/landmarks")
def upload_landmarks(trip_id):
    db = get_db()
    _trip_or_404(db, trip_id)
    items, errors = excel.parse_landmarks(_uploaded_file())
    if not items:
        return jsonify({"error": "No se ha podido leer ningún hito", "errors": errors}), 400

    added = updated = 0
    for order, it in enumerate(items):
        existing = row(db.execute("SELECT * FROM landmarks WHERE trip_id = ? AND name_key = ?",
                                  (trip_id, it["name_key"])))
        if existing is None:
            geocode_status = "manual" if it["lat"] is not None else "pendiente"
            cur = db.execute(
                "INSERT INTO landmarks (trip_id, name, architect, city, address, year, notes, lat, lon, "
                "geocode_status, url_archdaily, url_av, url_image1, url_image2, sort_order, name_key, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trip_id, it["name"], it["architect"], it["city"], it["address"], it["year"], it["notes"],
                 it["lat"], it["lon"], geocode_status, it["url_archdaily"], it["url_av"],
                 it["url_image1"], it["url_image2"], order, it["name_key"], it.get("status") or "pendiente"),
            )
            audit.log(db, "create_landmark", trip_id, cur.lastrowid, it["name"], new="plantilla")
            added += 1
        else:
            # Keep curation state and (unless the row now says otherwise) the
            # location; re-geocode only if the location hints changed.
            lat, lon, geocode_status = existing["lat"], existing["lon"], existing["geocode_status"]
            drive_source = existing["drive_source"]
            if it["lat"] is not None:
                if (it["lat"], it["lon"]) != (lat, lon):
                    lat, lon, geocode_status, drive_source = it["lat"], it["lon"], "manual", None
            elif geocode_status != "manual" and (
                excel.normalise(it["city"]) != excel.normalise(existing["city"])
                or excel.normalise(it["address"]) != excel.normalise(existing["address"])
                or geocode_status == "fallido"
            ):
                lat, lon, geocode_status, drive_source = None, None, "pendiente", None
            db.execute(
                "UPDATE landmarks SET name = ?, architect = ?, city = ?, address = ?, year = ?, "
                "notes = COALESCE(?, notes), lat = ?, lon = ?, geocode_status = ?, drive_source = ?, "
                "url_archdaily = COALESCE(?, url_archdaily), url_av = COALESCE(?, url_av), "
                "url_image1 = COALESCE(?, url_image1), url_image2 = COALESCE(?, url_image2), "
                "sort_order = ? WHERE id = ?",
                (it["name"], it["architect"], it["city"], it["address"], it["year"], it["notes"],
                 lat, lon, geocode_status, drive_source, it["url_archdaily"], it["url_av"],
                 it["url_image1"], it["url_image2"], order, existing["id"]),
            )
            # a status in the sheet only counts while the row was never curated here
            if it.get("status") and existing["status"] == "pendiente":
                db.execute("UPDATE landmarks SET status = ? WHERE id = ?", (it["status"], existing["id"]))
            after = row(db.execute("SELECT * FROM landmarks WHERE id = ?", (existing["id"],)))
            if any(after[k] != existing[k] for k in audit.UPLOAD_FIELDS):
                audit.log(db, "upload_update", trip_id, existing["id"], existing["name"], new="plantilla",
                          snapshot={k: existing[k] for k in audit.UPLOAD_FIELDS})
            updated += 1
    db.commit()
    return jsonify({"added": added, "updated": updated, "errors": errors})


@api.delete("/trips/<int:trip_id>/landmarks")
def clear_landmarks(trip_id):
    db = get_db()
    _trip_or_404(db, trip_id)
    snaps = [audit.landmark_snapshot(db, r["id"]) for r in
             db.execute("SELECT id FROM landmarks WHERE trip_id = ?", (trip_id,)).fetchall()]
    if snaps:
        audit.log(db, "clear_landmarks", trip_id, old=f"{len(snaps)} hitos", snapshot=snaps)
    db.execute("DELETE FROM landmarks WHERE trip_id = ?", (trip_id,))
    db.commit()
    return "", 204


# -------------------------------------------------------------- landmarks

def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        abort(400, description="Latitud/longitud no válidas")


@api.patch("/landmarks/<int:lm_id>")
def patch_landmark(lm_id):
    db = get_db()
    lm = _landmark_or_404(db, lm_id)
    body = request.get_json(silent=True) or {}
    sets, params, journal = [], [], []          # journal: audit.log kwargs, written once the UPDATE succeeds
    coords_before = {"lat": lm["lat"], "lon": lm["lon"], "geocode_status": lm["geocode_status"]}

    if "status" in body:
        if body["status"] not in STATUSES:
            abort(400, description="Estado no válido")
        sets.append("status = ?")
        params.append(body["status"])
        if body["status"] != lm["status"]:
            journal.append(dict(action="status", field="status", old=lm["status"], new=body["status"]))

    for field in EDITABLE:
        if field in body:
            value = (body[field] or "").strip() if isinstance(body[field], str) else body[field]
            if field in ("name", "architect", "city") and not value:
                abort(400, description=f"El campo {field} no puede quedar vacío")
            sets.append(f"{field} = ?")
            params.append(value or None)
            if (value or None) != lm[field]:
                journal.append(dict(action="edit", field=field, old=lm[field], new=value or None))
    if "name" in body or "architect" in body:
        sets.append("name_key = ?")
        params.append(excel.landmark_key(body.get("name", lm["name"]), body.get("architect", lm["architect"])))

    if "lat" in body or "lon" in body:
        lat, lon = _to_float(body.get("lat", lm["lat"])), _to_float(body.get("lon", lm["lon"]))
        if (lat is None) != (lon is None):
            abort(400, description="Latitud y longitud deben ir juntas")
        if lat is None:
            sets += ["lat = NULL", "lon = NULL", "geocode_status = 'pendiente'", "drive_source = NULL",
                     "nearest_stop_id = NULL", "drive_minutes = NULL", "drive_km = NULL"]
            if lm["lat"] is not None:
                journal.append(dict(action="edit", field="coords", snapshot=coords_before,
                                    old=audit.coords_text(**coords_before), new=audit.coords_text(None, None)))
        elif (lat, lon) != (lm["lat"], lm["lon"]):
            sets += ["lat = ?", "lon = ?", "geocode_status = 'manual'", "drive_source = NULL"]
            params += [lat, lon]
            journal.append(dict(action="edit", field="coords", snapshot=coords_before,
                                old=audit.coords_text(**coords_before), new=audit.coords_text(lat, lon, "manual")))

    if body.get("retry_links"):
        sets.append("links_status = 'pendiente'")
        journal.append(dict(action="retry_links"))
    if body.get("retry_geocode"):
        sets += ["lat = NULL", "lon = NULL", "geocode_status = 'pendiente'", "drive_source = NULL",
                 "nearest_stop_id = NULL", "drive_minutes = NULL", "drive_km = NULL"]
        journal.append(dict(action="retry_geocode", old=audit.coords_text(**coords_before)))

    if sets:
        try:
            db.execute(f"UPDATE landmarks SET {', '.join(sets)} WHERE id = ?", (*params, lm_id))
        except Exception as exc:  # UNIQUE (trip_id, name_key)
            if "UNIQUE" in str(exc):
                abort(400, description="Ya existe un hito con ese edificio y arquitecto")
            raise
        for entry in journal:
            audit.log(db, trip_id=lm["trip_id"], landmark_id=lm_id, landmark_name=lm["name"], **entry)
        db.commit()
    return jsonify(_landmark_or_404(db, lm_id))


@api.post("/landmarks/<int:lm_id>/images/refresh")
def refresh_images(lm_id):
    """Drop the fetched images and queue a new search (the enrich loop does the work)."""
    db = get_db()
    lm = _landmark_or_404(db, lm_id)
    db.execute("DELETE FROM landmark_images WHERE landmark_id = ? AND source != 'manual'", (lm_id,))
    db.execute("UPDATE landmarks SET images_status = 'pendiente' WHERE id = ?", (lm_id,))
    audit.log(db, "refresh_images", lm["trip_id"], lm_id, lm["name"])
    db.commit()
    return jsonify(_landmark_or_404(db, lm_id))


@api.post("/landmarks/<int:lm_id>/images")
def add_image(lm_id):
    """A photo/drawing added by hand: JSON {url, kind} or multipart file + kind."""
    db = get_db()
    lm = _landmark_or_404(db, lm_id)
    kind = (request.form.get("kind") or (request.get_json(silent=True) or {}).get("kind") or "foto")
    if kind not in ("foto", "plano"):
        abort(400, description="Tipo no válido")
    f = request.files.get("file")
    if f is not None and f.filename:
        try:
            url, thumb = uploads.store_file(lm_id, f.read())
        except ValueError as exc:
            abort(400, description=str(exc))
        title, page_url = f.filename, None
    else:
        url = ((request.get_json(silent=True) or {}).get("url") or request.form.get("url") or "").strip()
        if not uploads.is_http_url(url):
            abort(400, description="Pega una dirección que empiece por http:// o https://, o elige un archivo")
        thumb, title, page_url = url, url.rsplit("/", 1)[-1][:120] or "imagen", url
    pos = db.execute("SELECT COALESCE(MIN(position), 0) - 1 FROM landmark_images WHERE landmark_id = ?",
                     (lm_id,)).fetchone()[0]
    cur = db.execute("INSERT INTO landmark_images (landmark_id, kind, url, thumb, title, page_url, source, position) "
                     "VALUES (?, ?, ?, ?, ?, ?, 'manual', ?)", (lm_id, kind, url, thumb, title, page_url, pos))
    audit.log(db, "image_add", lm["trip_id"], lm_id, lm["name"], new=title,
              snapshot=row(db.execute("SELECT * FROM landmark_images WHERE id = ?", (cur.lastrowid,))))
    db.commit()
    return jsonify(_landmark_or_404(db, lm_id)), 201


@api.put("/landmarks/<int:lm_id>/images/order")
def order_images(lm_id):
    """{ids: [...]} in the wanted order (any kind); positions follow the list."""
    db = get_db()
    lm = _landmark_or_404(db, lm_id)
    ids = (request.get_json(silent=True) or {}).get("ids") or []
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        abort(400, description="Lista de imágenes no válida")
    before = [[im["id"], im["position"]] for im in lm["images"]]
    for pos, img_id in enumerate(ids):
        db.execute("UPDATE landmark_images SET position = ? WHERE id = ? AND landmark_id = ?", (pos, img_id, lm_id))
    after = {r["id"]: r["position"] for r in db.execute(
        "SELECT id, position FROM landmark_images WHERE landmark_id = ?", (lm_id,)).fetchall()}
    if any(after.get(i) != p for i, p in before):
        audit.log(db, "image_order", lm["trip_id"], lm_id, lm["name"], snapshot=before)
    db.commit()
    return jsonify(_landmark_or_404(db, lm_id))


@api.patch("/landmarks/<int:lm_id>/images/<int:img_id>")
def patch_image(lm_id, img_id):
    """Reclassify a picture: {kind: foto|plano}."""
    db = get_db()
    lm = _landmark_or_404(db, lm_id)
    kind = (request.get_json(silent=True) or {}).get("kind")
    if kind not in ("foto", "plano"):
        abort(400, description="Tipo no válido")
    im = row(db.execute("SELECT * FROM landmark_images WHERE id = ? AND landmark_id = ?", (img_id, lm_id)))
    if im and im["kind"] != kind:
        db.execute("UPDATE landmark_images SET kind = ? WHERE id = ?", (kind, img_id))
        audit.log(db, "image_kind", lm["trip_id"], lm_id, lm["name"], old=im["kind"], new=kind,
                  snapshot={"id": img_id, "title": im["title"]})
        db.commit()
    return jsonify(_landmark_or_404(db, lm_id))


@api.delete("/landmarks/<int:lm_id>/images/<int:img_id>")
def delete_image(lm_id, img_id):
    db = get_db()
    lm = _landmark_or_404(db, lm_id)
    im = row(db.execute("SELECT * FROM landmark_images WHERE id = ? AND landmark_id = ?", (img_id, lm_id)))
    if im:
        # uploaded files are kept on disk so the admin can undo the deletion
        db.execute("DELETE FROM landmark_images WHERE id = ?", (img_id,))
        audit.log(db, "image_delete", lm["trip_id"], lm_id, lm["name"], old=im["title"], snapshot=im)
        db.commit()
    return jsonify(_landmark_or_404(db, lm_id))


@api.delete("/landmarks/<int:lm_id>")
def delete_landmark(lm_id):
    db = get_db()
    lm = _landmark_or_404(db, lm_id)
    audit.log(db, "delete_landmark", lm["trip_id"], lm_id, lm["name"], old=lm["status"],
              snapshot=audit.landmark_snapshot(db, lm_id))
    db.execute("DELETE FROM landmarks WHERE id = ?", (lm_id,))
    db.commit()
    return "", 204


# --------------------------------------------------------------- itinerary
# Days of the trip, each with an ordered list of landmarks and free text blocks.

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _day_or_404(db, day_id):
    d = row(db.execute("SELECT * FROM trip_days WHERE id = ?", (day_id,)))
    if d is None:
        abort(404, description="Día no encontrado")
    return d


def _item_or_404(db, item_id):
    it = row(db.execute("SELECT i.*, d.trip_id FROM day_items i JOIN trip_days d ON d.id = i.day_id "
                        "WHERE i.id = ?", (item_id,)))
    if it is None:
        abort(404, description="Elemento no encontrado")
    return it


def _clean_date(value):
    value = (value or "").strip()
    if value and not DATE_RE.match(value):
        abort(400, description="La fecha debe ser AAAA-MM-DD")
    return value or None


def _clean_time(value):
    value = (value or "").strip()
    if value and not TIME_RE.match(value):
        abort(400, description="La hora debe ser HH:MM")
    return value or None


def _day_view(db, day_id):
    d = row(db.execute("SELECT d.*, s.city AS stop_city FROM trip_days d LEFT JOIN route_stops s ON s.id = d.stop_id "
                       "WHERE d.id = ?", (day_id,)))
    d["items"] = rows(db.execute("SELECT * FROM day_items WHERE day_id = ? ORDER BY position, id", (day_id,)))
    return d


def _day_label(day):
    return day.get("date") or day.get("title") or f"día {day['id']}"


def _item_label(db, item):
    if item["kind"] == "hito":
        lm = row(db.execute("SELECT name FROM landmarks WHERE id = ?", (item["landmark_id"],)))
        return (lm or {}).get("name") or f"hito {item['landmark_id']}"
    return (item["text"] or "")[:60]


@api.post("/trips/<int:trip_id>/days")
def create_day(trip_id):
    db = get_db()
    _trip_or_404(db, trip_id)
    body = request.get_json(silent=True) or {}
    date = _clean_date(body.get("date"))
    stop_id = body.get("stop_id") or None
    if stop_id and not row(db.execute("SELECT 1 AS x FROM route_stops WHERE id = ? AND trip_id = ?", (stop_id, trip_id))):
        abort(400, description="Esa parada no es de este viaje")
    pos = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM trip_days WHERE trip_id = ?", (trip_id,)).fetchone()[0]
    cur = db.execute("INSERT INTO trip_days (trip_id, date, position, stop_id, title, notes) VALUES (?, ?, ?, ?, ?, ?)",
                     (trip_id, date, pos, stop_id, (body.get("title") or "").strip() or None,
                      (body.get("notes") or "").strip() or None))
    audit.log(db, "day_create", trip_id, new=date or f"día {cur.lastrowid}", snapshot={"id": cur.lastrowid})
    db.commit()
    return jsonify(_day_view(db, cur.lastrowid)), 201


@api.patch("/days/<int:day_id>")
def patch_day(day_id):
    db = get_db()
    day = _day_or_404(db, day_id)
    body = request.get_json(silent=True) or {}
    for field in ("date", "stop_id", "title", "notes"):
        if field not in body:
            continue
        if field == "date":
            value = _clean_date(body["date"])
        elif field == "stop_id":
            value = body["stop_id"] or None
            if value and not row(db.execute("SELECT 1 AS x FROM route_stops WHERE id = ? AND trip_id = ?",
                                            (value, day["trip_id"]))):
                abort(400, description="Esa parada no es de este viaje")
        else:
            value = (body[field] or "").strip() or None
        if value != day[field]:
            db.execute(f"UPDATE trip_days SET {field} = ? WHERE id = ?", (value, day_id))
            audit.log(db, "day_edit", day["trip_id"], field=field, old=day[field], new=value,
                      snapshot={"day_id": day_id}, landmark_name=_day_label(day))
    db.commit()
    return jsonify(_day_view(db, day_id))


@api.delete("/days/<int:day_id>")
def delete_day(day_id):
    db = get_db()
    day = _day_or_404(db, day_id)
    snap = dict(day, items=rows(db.execute("SELECT * FROM day_items WHERE day_id = ? ORDER BY position, id", (day_id,))))
    audit.log(db, "day_delete", day["trip_id"], old=_day_label(day), snapshot=snap, landmark_name=_day_label(day))
    db.execute("DELETE FROM trip_days WHERE id = ?", (day_id,))
    db.commit()
    return "", 204


@api.post("/days/<int:day_id>/items")
def add_item(day_id):
    db = get_db()
    day = _day_or_404(db, day_id)
    body = request.get_json(silent=True) or {}
    kind = body.get("kind") or ("hito" if body.get("landmark_id") else "nota")
    if kind not in ("hito", "nota"):
        abort(400, description="Tipo no válido")
    at_time = _clean_time(body.get("at_time"))
    landmark_id, text = None, None
    if kind == "hito":
        landmark_id = body.get("landmark_id")
        if not row(db.execute("SELECT 1 AS x FROM landmarks WHERE id = ? AND trip_id = ?", (landmark_id, day["trip_id"]))):
            abort(400, description="Ese hito no es de este viaje")
    else:
        text = (body.get("text") or "").strip()
        if not text:
            abort(400, description="Escribe el texto de la nota")
    pos = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM day_items WHERE day_id = ?", (day_id,)).fetchone()[0]
    cur = db.execute("INSERT INTO day_items (day_id, position, at_time, kind, landmark_id, text) VALUES (?, ?, ?, ?, ?, ?)",
                     (day_id, pos, at_time, kind, landmark_id, text))
    item = row(db.execute("SELECT * FROM day_items WHERE id = ?", (cur.lastrowid,)))
    audit.log(db, "item_add", day["trip_id"], landmark_id, _item_label(db, item), new=_day_label(day), snapshot=item)
    db.commit()
    return jsonify(_day_view(db, day_id)), 201


@api.patch("/items/<int:item_id>")
def patch_item(item_id):
    """Time, note text, or move to another day ({day_id})."""
    db = get_db()
    item = _item_or_404(db, item_id)
    body = request.get_json(silent=True) or {}
    label = _item_label(db, item)
    if "at_time" in body:
        value = _clean_time(body["at_time"])
        if value != item["at_time"]:
            db.execute("UPDATE day_items SET at_time = ? WHERE id = ?", (value, item_id))
            audit.log(db, "item_edit", item["trip_id"], item["landmark_id"], label, field="at_time",
                      old=item["at_time"], new=value, snapshot={"item_id": item_id})
    if "text" in body and item["kind"] == "nota":
        value = (body["text"] or "").strip()
        if not value:
            abort(400, description="Escribe el texto de la nota")
        if value != item["text"]:
            db.execute("UPDATE day_items SET text = ? WHERE id = ?", (value, item_id))
            audit.log(db, "item_edit", item["trip_id"], None, label, field="text",
                      old=item["text"], new=value, snapshot={"item_id": item_id})
    if "needs_confirm" in body:
        value = 1 if body["needs_confirm"] else 0
        if value != item["needs_confirm"]:
            db.execute("UPDATE day_items SET needs_confirm = ? WHERE id = ?", (value, item_id))
            audit.log(db, "item_edit", item["trip_id"], item["landmark_id"], label, field="needs_confirm",
                      old=item["needs_confirm"], new=value, snapshot={"item_id": item_id})
    if "day_id" in body and body["day_id"] != item["day_id"]:
        target = _day_or_404(db, body["day_id"])
        if target["trip_id"] != item["trip_id"]:
            abort(400, description="Ese día no es de este viaje")
        pos = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM day_items WHERE day_id = ?",
                         (target["id"],)).fetchone()[0]
        db.execute("UPDATE day_items SET day_id = ?, position = ? WHERE id = ?", (target["id"], pos, item_id))
        audit.log(db, "item_move", item["trip_id"], item["landmark_id"], label, field="day_id",
                  old=item["day_id"], new=target["id"], snapshot={"item_id": item_id, "position": item["position"]})
    db.commit()
    it = row(db.execute("SELECT day_id FROM day_items WHERE id = ?", (item_id,)))
    return jsonify(_day_view(db, it["day_id"]))


@api.delete("/items/<int:item_id>")
def delete_item(item_id):
    db = get_db()
    item = _item_or_404(db, item_id)
    snap = {k: item[k] for k in ("id", "day_id", "position", "at_time", "kind", "landmark_id", "text")}
    audit.log(db, "item_delete", item["trip_id"], item["landmark_id"], _item_label(db, item), snapshot=snap)
    db.execute("DELETE FROM day_items WHERE id = ?", (item_id,))
    db.commit()
    return jsonify(_day_view(db, item["day_id"]))


@api.put("/days/<int:day_id>/items/order")
def order_items(day_id):
    """{ids: [...]} in the wanted order, all of this day."""
    db = get_db()
    day = _day_or_404(db, day_id)
    ids = (request.get_json(silent=True) or {}).get("ids") or []
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        abort(400, description="Lista de elementos no válida")
    before = [[r["id"], r["position"]] for r in
              db.execute("SELECT id, position FROM day_items WHERE day_id = ?", (day_id,)).fetchall()]
    for pos, item_id in enumerate(ids, start=1):
        db.execute("UPDATE day_items SET position = ? WHERE id = ? AND day_id = ?", (pos, item_id, day_id))
    after = {r["id"]: r["position"] for r in
             db.execute("SELECT id, position FROM day_items WHERE day_id = ?", (day_id,)).fetchall()}
    if any(after.get(i) != p for i, p in before):
        audit.log(db, "item_order", day["trip_id"], landmark_name=_day_label(day), snapshot=before)
    db.commit()
    return jsonify(_day_view(db, day_id))


# ----------------------------------------------------------------- enrich

@api.post("/trips/<int:trip_id>/enrich/next")
def enrich_next(trip_id):
    db = get_db()
    _trip_or_404(db, trip_id)
    result = enrich.step(trip_id)
    if result.get("item") and result["item"]["kind"] == "landmark":
        result["landmark"] = _landmark_or_404(db, result["item"]["id"])
    elif result.get("item") and result["item"]["kind"] == "stop":
        result["stop"] = row(db.execute("SELECT * FROM route_stops WHERE id = ?", (result["item"]["id"],)))
    return jsonify(result)


# ---------------------------------------------------------------- exports

@api.get("/trips/<int:trip_id>/export/html")
def export_html(trip_id):
    db = get_db()
    trip = _trip_or_404(db, trip_id)
    html, filename = export.standalone_html(_trip_payload(db, trip))
    return send_file(io.BytesIO(html.encode("utf-8")), mimetype="text/html",
                     as_attachment=True, download_name=filename)


@api.get("/trips/<int:trip_id>/export/itinerario")
def export_itinerary(trip_id):
    """Day-by-day itinerary as a PDF, ready to print or to send to the students.
    `?fotos=1` adds each landmark's picture (slower: the images are fetched on the fly)."""
    db = get_db()
    trip = _trip_or_404(db, trip_id)
    data, filename = export.itinerary_pdf(_trip_payload(db, trip), gallery="fotos" in request.args)
    return send_file(io.BytesIO(data), mimetype="application/pdf",
                     as_attachment=True, download_name=filename)


@api.get("/trips/<int:trip_id>/export/obsidian")
def export_obsidian(trip_id):
    db = get_db()
    trip = _trip_or_404(db, trip_id)
    data, filename = export.obsidian_zip(_trip_payload(db, trip))
    return send_file(io.BytesIO(data), mimetype="application/zip",
                     as_attachment=True, download_name=filename)
