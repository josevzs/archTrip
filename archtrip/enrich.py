"""One unit of enrichment work per call: geocode a stop, geocode a landmark,
compute a landmark's drive time from its nearest route stop, or fetch its
photos/drawings (Wikidata + Commons).

The frontend calls this in a loop until `remaining` is 0, so there are no
background threads or queues to manage."""
import re

import requests

from . import geo, images, links
from .db import get_db, row, rows


def _located_stops(db, trip_id):
    return rows(db.execute(
        "SELECT id, position, city, lat, lon FROM route_stops "
        "WHERE trip_id = ? AND lat IS NOT NULL AND lon IS NOT NULL ORDER BY position",
        (trip_id,),
    ))


def pending_counts(db, trip_id):
    stops = db.execute(
        "SELECT COUNT(*) FROM route_stops WHERE trip_id = ? AND geocode_status = 'pendiente'",
        (trip_id,),
    ).fetchone()[0]
    geocode = db.execute(
        "SELECT COUNT(*) FROM landmarks WHERE trip_id = ? AND geocode_status = 'pendiente'",
        (trip_id,),
    ).fetchone()[0]
    has_located_stop = db.execute(
        "SELECT 1 FROM route_stops WHERE trip_id = ? AND lat IS NOT NULL LIMIT 1", (trip_id,)
    ).fetchone() is not None
    drive = 0
    if has_located_stop:
        drive = db.execute(
            "SELECT COUNT(*) FROM landmarks WHERE trip_id = ? AND lat IS NOT NULL "
            "AND drive_source IS NULL",
            (trip_id,),
        ).fetchone()[0]
    imgs = db.execute(
        "SELECT COUNT(*) FROM landmarks WHERE trip_id = ? AND images_status = 'pendiente'", (trip_id,)
    ).fetchone()[0]
    lnk = db.execute(
        "SELECT COUNT(*) FROM landmarks WHERE trip_id = ? AND links_status = 'pendiente'", (trip_id,)
    ).fetchone()[0]
    return {"stops": stops, "geocode": geocode, "drive": drive, "images": imgs, "links": lnk,
            "total": stops + geocode + drive + imgs + lnk, "has_route": has_located_stop}


def _geocode_stop(db, stop):
    query = ", ".join(p for p in (stop["city"], stop["country"]) if p)
    result = geo.nominatim_geocode(query, prefer_settlement=True)
    if result:
        db.execute("UPDATE route_stops SET lat = ?, lon = ?, geocode_status = 'ok' WHERE id = ?",
                   (result[0], result[1], stop["id"]))
    else:
        db.execute("UPDATE route_stops SET geocode_status = 'fallido' WHERE id = ?", (stop["id"],))
    db.commit()
    return {"kind": "stop", "id": stop["id"], "ok": bool(result)}


def simplified_name(name):
    """'Tower of the Sun (Expo Park)' -> 'Tower of the Sun'; 'St. Mary's Cathedral, Tamatsukuri'
    -> 'St. Mary's Cathedral'. Nominatim matches names, not descriptions. None if unchanged."""
    s = re.sub(r"\s*\([^)]*\)", "", name).split(",")[0]
    s = re.sub(r"\s+", " ", s).strip()
    return s if s and s != name.strip() else None


def _geocode_landmark(db, lm):
    # Most specific query first (address, else building name), then the name
    # without parentheticals/qualifiers, then the city centre so a drive time
    # can still be shown, flagged as approximate.
    specific = ", ".join(p for p in (lm["address"] or lm["name"], lm["city"]) if p)
    result, status = geo.nominatim_geocode(specific), "exacta"
    if result is None and lm["address"]:
        result = geo.nominatim_geocode(f"{lm['name']}, {lm['city']}")
    simple = simplified_name(lm["name"])
    if result is None and simple:
        result = geo.nominatim_geocode(f"{simple}, {lm['city']}")
    if result is None:
        result, status = geo.nominatim_geocode(lm["city"], prefer_settlement=True), "ciudad"
    if result is None:
        status = "fallido"
        db.execute("UPDATE landmarks SET geocode_status = 'fallido' WHERE id = ?", (lm["id"],))
    else:
        db.execute(
            "UPDATE landmarks SET lat = ?, lon = ?, geocode_status = ?, drive_source = NULL WHERE id = ?",
            (result[0], result[1], status, lm["id"]),
        )
    db.commit()
    return {"kind": "landmark", "id": lm["id"], "ok": status != "fallido", "geocode_status": status}


def _route_landmark(db, lm, stops):
    stop, straight_km = geo.nearest_stop(lm["lat"], lm["lon"], stops)
    if stop is None:
        return None
    try:
        drive = geo.osrm_drive(stop["lat"], stop["lon"], lm["lat"], lm["lon"])
    except requests.RequestException:
        drive = None
    source = "osrm"
    if drive is None:
        drive, source = geo.estimate_drive(straight_km), "estimado"
    db.execute(
        "UPDATE landmarks SET nearest_stop_id = ?, drive_minutes = ?, drive_km = ?, drive_source = ? "
        "WHERE id = ?",
        (stop["id"], drive[0], drive[1], source, lm["id"]),
    )
    db.commit()
    return {"kind": "landmark", "id": lm["id"], "ok": True, "drive_source": source}


def _fetch_images(db, lm):
    found = images.fetch_images(lm["name"], lm["city"], lm["lat"], lm["lon"])
    db.execute("DELETE FROM landmark_images WHERE landmark_id = ? AND source != 'manual'", (lm["id"],))
    db.executemany(
        "INSERT INTO landmark_images (landmark_id, kind, url, thumb, title, page_url, source, position) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(lm["id"], im["kind"], im["url"], im["thumb"], im["title"], im["page_url"], im["source"], i)
         for i, im in enumerate(found["images"])],
    )
    status = "ok" if found["images"] else "ninguna"
    db.execute("UPDATE landmarks SET images_status = ?, wikidata_id = ?, wikipedia_url = ? WHERE id = ?",
               (status, found["wikidata_id"], found["wikipedia_url"], lm["id"]))
    # Wikidata knows exactly where the building is; use it when Nominatim only found the city.
    if found["lat"] is not None and lm["geocode_status"] in ("ciudad", "fallido"):
        db.execute("UPDATE landmarks SET lat = ?, lon = ?, geocode_status = 'exacta', drive_source = NULL "
                   "WHERE id = ?", (found["lat"], found["lon"], lm["id"]))
    db.commit()
    return {"kind": "landmark", "id": lm["id"], "ok": status == "ok", "images": len(found["images"])}


def _find_links(db, lm):
    """Fill url_archdaily / url_av when empty, from the sites' own search. Never overwrites a URL
    the professor typed."""
    found = {}
    stop = row(db.execute("SELECT country FROM route_stops WHERE id = ?", (lm["nearest_stop_id"],))) if lm["nearest_stop_id"] else None
    country = (stop or {}).get("country") or ""
    if not lm["url_archdaily"]:
        found["url_archdaily"] = links.tolerant(links.find_archdaily, lm["name"], lm["architect"], lm["city"], country)
    if not lm["url_av"]:
        found["url_av"] = links.tolerant(links.find_av, lm["name"], lm["architect"], lm["city"])
    for col, url in found.items():
        if url:
            db.execute(f"UPDATE landmarks SET {col} = ? WHERE id = ?", (url, lm["id"]))
    db.execute("UPDATE landmarks SET links_status = 'ok' WHERE id = ?", (lm["id"],))
    db.commit()
    return {"kind": "landmark", "id": lm["id"], "ok": True, "links": sum(1 for u in found.values() if u)}


def step(trip_id):
    """Do one unit of work. -> {done, remaining, item, error?}"""
    db = get_db()
    item = None
    try:
        stop = row(db.execute(
            "SELECT * FROM route_stops WHERE trip_id = ? AND geocode_status = 'pendiente' "
            "ORDER BY position LIMIT 1", (trip_id,)))
        if stop:
            item = _geocode_stop(db, stop)
        else:
            lm = row(db.execute(
                "SELECT * FROM landmarks WHERE trip_id = ? AND geocode_status = 'pendiente' "
                "ORDER BY sort_order, id LIMIT 1", (trip_id,)))
            if lm:
                item = _geocode_landmark(db, lm)
            else:
                stops = _located_stops(db, trip_id)
                if stops:
                    lm = row(db.execute(
                        "SELECT * FROM landmarks WHERE trip_id = ? AND lat IS NOT NULL "
                        "AND drive_source IS NULL ORDER BY sort_order, id LIMIT 1", (trip_id,)))
                    if lm:
                        item = _route_landmark(db, lm, stops)
                if item is None:
                    lm = row(db.execute(
                        "SELECT * FROM landmarks WHERE trip_id = ? AND images_status = 'pendiente' "
                        "ORDER BY sort_order, id LIMIT 1", (trip_id,)))
                    if lm:
                        try:
                            item = _fetch_images(db, lm)
                        except requests.RequestException:
                            raise
                        except Exception:  # unexpected payload: don't let one landmark stall the loop
                            db.execute("UPDATE landmarks SET images_status = 'fallido' WHERE id = ?", (lm["id"],))
                            db.commit()
                            item = {"kind": "landmark", "id": lm["id"], "ok": False, "images": 0}
                if item is None:
                    lm = row(db.execute(
                        "SELECT * FROM landmarks WHERE trip_id = ? AND links_status = 'pendiente' "
                        "ORDER BY sort_order, id LIMIT 1", (trip_id,)))
                    if lm:
                        try:
                            item = _find_links(db, lm)
                        except requests.RequestException:
                            raise
                        except Exception:
                            db.execute("UPDATE landmarks SET links_status = 'ok' WHERE id = ?", (lm["id"],))
                            db.commit()
                            item = {"kind": "landmark", "id": lm["id"], "ok": False, "links": 0}
    except requests.RequestException as exc:
        counts = pending_counts(db, trip_id)
        return {"done": False, "remaining": counts["total"], "item": None,
                "error": f"Sin conexión con el servicio de mapas ({exc.__class__.__name__})."}

    counts = pending_counts(db, trip_id)
    return {"done": counts["total"] == 0, "remaining": counts["total"], "item": item,
            "counts": counts}
