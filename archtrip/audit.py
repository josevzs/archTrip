"""Editing sessions and a change journal.

A session is a client (cookie token + IP + browser) that changed something; it opens on the
first modifying request, stays open while changes keep coming, and counts as closed after
SESSION_GAP seconds of silence (nothing is stored for closing: it is derived from `last_ts`).
Every change records enough (old value or a full snapshot) to be undone by admin.revert()."""
import json
import secrets
import time
from datetime import datetime, timezone

from flask import g, request

from .db import get_db, row

SESSION_GAP = 3600           # seconds without changes after which a session counts as closed
COOKIE = "archtrip_sid"

# actions that carry no inverse (they only trigger automatic re-enrichment)
INFORMATIONAL = {"retry_geocode", "refresh_images", "retry_links", "restore"}

# what a template re-upload may overwrite on an existing landmark (snapshot for `upload_update`)
UPLOAD_FIELDS = ("name", "architect", "city", "address", "year", "notes", "lat", "lon", "geocode_status",
                 "url_archdaily", "url_av", "url_image1", "url_image2", "status", "sort_order", "name_key")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "?"


def session_id(db):
    """Session for the current request, opened on demand (cached in flask.g)."""
    if getattr(g, "audit_sid", None):
        return g.audit_sid
    token = request.cookies.get(COOKIE)
    if not token:
        token = secrets.token_urlsafe(18)
        g.audit_new_token = token          # __init__ sets the cookie after the request
    ts = time.time()
    last = row(db.execute("SELECT * FROM sessions WHERE token = ? ORDER BY id DESC LIMIT 1", (token,)))
    if last and ts - last["last_ts"] < SESSION_GAP:
        db.execute("UPDATE sessions SET last_at = ?, last_ts = ? WHERE id = ?", (now_iso(), ts, last["id"]))
        sid = last["id"]
    else:
        cur = db.execute(
            "INSERT INTO sessions (token, ip, user_agent, started_at, last_at, last_ts) VALUES (?, ?, ?, ?, ?, ?)",
            (token, client_ip(), (request.headers.get("User-Agent") or "")[:200], now_iso(), now_iso(), ts))
        sid = cur.lastrowid
    g.audit_sid = sid
    return sid


def log(db, action, trip_id=None, landmark_id=None, landmark_name=None, field=None, old=None, new=None,
        snapshot=None, revertible=True, revert_of=None):
    """Append one change to the journal (caller commits)."""
    sid = session_id(db)
    cur = db.execute(
        "INSERT INTO changes (session_id, at, trip_id, landmark_id, landmark_name, action, field, old_value, new_value, "
        "snapshot, revertible, revert_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, now_iso(), trip_id, landmark_id, landmark_name, action, field,
         _text(old), _text(new), json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None,
         1 if revertible and action not in INFORMATIONAL else 0, revert_of))
    return cur.lastrowid


def _text(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def landmark_snapshot(db, lm_id):
    """Full copy of a landmark and its pictures, enough to re-create it."""
    lm = row(db.execute("SELECT * FROM landmarks WHERE id = ?", (lm_id,)))
    if lm is None:
        return None
    lm["images"] = [dict(r) for r in db.execute(
        "SELECT * FROM landmark_images WHERE landmark_id = ? ORDER BY position, id", (lm_id,)).fetchall()]
    return lm


def trip_snapshot(db, trip_id):
    """Everything in a trip (row, stops, landmarks with pictures), enough to re-create it."""
    t = row(db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)))
    stops = [dict(r) for r in db.execute("SELECT * FROM route_stops WHERE trip_id = ? ORDER BY position",
                                         (trip_id,)).fetchall()]
    lms = [landmark_snapshot(db, r["id"]) for r in
           db.execute("SELECT id FROM landmarks WHERE trip_id = ?", (trip_id,)).fetchall()]
    return {"trip": t, "stops": stops, "landmarks": lms}


def coords_text(lat, lon, geocode_status=None):
    """Human-readable old/new value for a coordinates change."""
    if lat is None:
        return "sin coordenadas"
    return f"{lat:.5f}, {lon:.5f}" + (f" ({geocode_status})" if geocode_status else "")


def is_open(session):
    return time.time() - session["last_ts"] < SESSION_GAP
