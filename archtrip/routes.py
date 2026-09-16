import io

from flask import Blueprint, abort, jsonify, request, send_file

from . import enrich, excel, export, prompt, uploads
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


def _trip_payload(db, trip):
    stops = rows(db.execute("SELECT * FROM route_stops WHERE trip_id = ? ORDER BY position",
                            (trip["id"],)))
    landmarks = _attach_images(db, rows(db.execute(
        LANDMARK_SELECT + " WHERE l.trip_id = ? ORDER BY l.sort_order, l.id", (trip["id"],))))
    return {"trip": trip, "stops": stops, "landmarks": landmarks,
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
    db.commit()
    return jsonify(row(db.execute("SELECT * FROM trips WHERE id = ?", (cur.lastrowid,)))), 201


@api.get("/trips/<int:trip_id>")
def get_trip(trip_id):
    db = get_db()
    return jsonify(_trip_payload(db, _trip_or_404(db, trip_id)))


@api.patch("/trips/<int:trip_id>")
def rename_trip(trip_id):
    db = get_db()
    _trip_or_404(db, trip_id)
    name = (request.get_json(silent=True) or {}).get("name", "").strip()
    if not name:
        abort(400, description="El viaje necesita un nombre")
    db.execute("UPDATE trips SET name = ? WHERE id = ?", (name, trip_id))
    db.commit()
    return jsonify(row(db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,))))


@api.delete("/trips/<int:trip_id>")
def delete_trip(trip_id):
    db = get_db()
    _trip_or_404(db, trip_id)
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
            db.execute(
                "INSERT INTO landmarks (trip_id, name, architect, city, address, year, notes, lat, lon, "
                "geocode_status, url_archdaily, url_av, url_image1, url_image2, sort_order, name_key, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trip_id, it["name"], it["architect"], it["city"], it["address"], it["year"], it["notes"],
                 it["lat"], it["lon"], geocode_status, it["url_archdaily"], it["url_av"],
                 it["url_image1"], it["url_image2"], order, it["name_key"], it.get("status") or "pendiente"),
            )
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
            updated += 1
    db.commit()
    return jsonify({"added": added, "updated": updated, "errors": errors})


@api.delete("/trips/<int:trip_id>/landmarks")
def clear_landmarks(trip_id):
    db = get_db()
    _trip_or_404(db, trip_id)
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
    sets, params = [], []

    if "status" in body:
        if body["status"] not in STATUSES:
            abort(400, description="Estado no válido")
        sets.append("status = ?")
        params.append(body["status"])

    for field in EDITABLE:
        if field in body:
            value = (body[field] or "").strip() if isinstance(body[field], str) else body[field]
            if field in ("name", "architect", "city") and not value:
                abort(400, description=f"El campo {field} no puede quedar vacío")
            sets.append(f"{field} = ?")
            params.append(value or None)
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
        elif (lat, lon) != (lm["lat"], lm["lon"]):
            sets += ["lat = ?", "lon = ?", "geocode_status = 'manual'", "drive_source = NULL"]
            params += [lat, lon]

    if body.get("retry_links"):
        sets.append("links_status = 'pendiente'")
    if body.get("retry_geocode"):
        sets += ["lat = NULL", "lon = NULL", "geocode_status = 'pendiente'", "drive_source = NULL",
                 "nearest_stop_id = NULL", "drive_minutes = NULL", "drive_km = NULL"]

    if sets:
        try:
            db.execute(f"UPDATE landmarks SET {', '.join(sets)} WHERE id = ?", (*params, lm_id))
        except Exception as exc:  # UNIQUE (trip_id, name_key)
            if "UNIQUE" in str(exc):
                abort(400, description="Ya existe un hito con ese edificio y arquitecto")
            raise
        db.commit()
    return jsonify(_landmark_or_404(db, lm_id))


@api.post("/landmarks/<int:lm_id>/images/refresh")
def refresh_images(lm_id):
    """Drop the fetched images and queue a new search (the enrich loop does the work)."""
    db = get_db()
    _landmark_or_404(db, lm_id)
    db.execute("DELETE FROM landmark_images WHERE landmark_id = ? AND source != 'manual'", (lm_id,))
    db.execute("UPDATE landmarks SET images_status = 'pendiente' WHERE id = ?", (lm_id,))
    db.commit()
    return jsonify(_landmark_or_404(db, lm_id))


@api.post("/landmarks/<int:lm_id>/images")
def add_image(lm_id):
    """A photo/drawing added by hand: JSON {url, kind} or multipart file + kind."""
    db = get_db()
    _landmark_or_404(db, lm_id)
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
    db.execute("INSERT INTO landmark_images (landmark_id, kind, url, thumb, title, page_url, source, position) "
               "VALUES (?, ?, ?, ?, ?, ?, 'manual', ?)", (lm_id, kind, url, thumb, title, page_url, pos))
    db.commit()
    return jsonify(_landmark_or_404(db, lm_id)), 201


@api.put("/landmarks/<int:lm_id>/images/order")
def order_images(lm_id):
    """{ids: [...]} in the wanted order (any kind); positions follow the list."""
    db = get_db()
    _landmark_or_404(db, lm_id)
    ids = (request.get_json(silent=True) or {}).get("ids") or []
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        abort(400, description="Lista de imágenes no válida")
    for pos, img_id in enumerate(ids):
        db.execute("UPDATE landmark_images SET position = ? WHERE id = ? AND landmark_id = ?", (pos, img_id, lm_id))
    db.commit()
    return jsonify(_landmark_or_404(db, lm_id))


@api.patch("/landmarks/<int:lm_id>/images/<int:img_id>")
def patch_image(lm_id, img_id):
    """Reclassify a picture: {kind: foto|plano}."""
    db = get_db()
    _landmark_or_404(db, lm_id)
    kind = (request.get_json(silent=True) or {}).get("kind")
    if kind not in ("foto", "plano"):
        abort(400, description="Tipo no válido")
    db.execute("UPDATE landmark_images SET kind = ? WHERE id = ? AND landmark_id = ?", (kind, img_id, lm_id))
    db.commit()
    return jsonify(_landmark_or_404(db, lm_id))


@api.delete("/landmarks/<int:lm_id>/images/<int:img_id>")
def delete_image(lm_id, img_id):
    db = get_db()
    _landmark_or_404(db, lm_id)
    im = row(db.execute("SELECT * FROM landmark_images WHERE id = ? AND landmark_id = ?", (img_id, lm_id)))
    if im:
        uploads.delete_files(im)
        db.execute("DELETE FROM landmark_images WHERE id = ?", (img_id,))
        db.commit()
    return jsonify(_landmark_or_404(db, lm_id))


@api.delete("/landmarks/<int:lm_id>")
def delete_landmark(lm_id):
    db = get_db()
    lm = _landmark_or_404(db, lm_id)
    for im in lm["images"]:
        uploads.delete_files(im)
    db.execute("DELETE FROM landmarks WHERE id = ?", (lm_id,))
    db.commit()
    return "", 204


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


@api.get("/trips/<int:trip_id>/export/obsidian")
def export_obsidian(trip_id):
    db = get_db()
    trip = _trip_or_404(db, trip_id)
    data, filename = export.obsidian_zip(_trip_payload(db, trip))
    return send_file(io.BytesIO(data), mimetype="application/zip",
                     as_attachment=True, download_name=filename)
