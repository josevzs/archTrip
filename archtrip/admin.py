"""Hidden admin area: editing sessions, the change journal with undo, and backups.
Password from ARCHTRIP_ADMIN_PASSWORD (default 'admin'); a signed cookie keeps the login."""
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, make_response, request, send_file

from . import audit, excel, export
from .db import get_db, row, rows

adm = Blueprint("admin", __name__)
ADMIN_COOKIE = "archtrip_admin"
_last_failed_login = 0.0


# ------------------------------------------------------------------ auth

def _secret():
    path = Path(current_app.config["DB_PATH"]).parent / "secret.key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32))
    return path.read_text().strip()


def _token():
    return hmac.new(_secret().encode(), b"archtrip-admin", hashlib.sha256).hexdigest()


def is_admin():
    return hmac.compare_digest(request.cookies.get(ADMIN_COOKIE, ""), _token())


def require_admin(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not is_admin():
            abort(401, description="Hace falta la contraseña de administración")
        return fn(*a, **k)
    return wrapper


@adm.errorhandler(400)
@adm.errorhandler(401)
@adm.errorhandler(404)
def _json_error(err):
    return jsonify({"error": err.description}), err.code


@adm.post("/login")
def login():
    global _last_failed_login
    password = os.environ.get("ARCHTRIP_ADMIN_PASSWORD", "admin")
    given = (request.get_json(silent=True) or {}).get("password", "")
    if not hmac.compare_digest(given, password):
        time.sleep(1.0 if time.time() - _last_failed_login < 10 else 0.2)   # blunt brute-force brake
        _last_failed_login = time.time()
        abort(401, description="Contraseña incorrecta")
    resp = make_response(jsonify({"ok": True}))
    resp.set_cookie(ADMIN_COOKIE, _token(), max_age=12 * 3600, httponly=True, samesite="Lax")
    return resp


@adm.post("/logout")
def logout():
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


@adm.get("/me")
def me():
    return jsonify({"admin": is_admin()})


# -------------------------------------------------------------- sessions

def _session_view(db, s):
    counts = row(db.execute(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN reverted_by IS NULL AND revertible = 1 AND revert_of IS NULL "
        "THEN 1 ELSE 0 END) AS pending, "
        "MIN(at) AS first_at, MAX(at) AS last_change FROM changes WHERE session_id = ?", (s["id"],)))
    trips = [r["name"] for r in db.execute(
        "SELECT DISTINCT t.name FROM changes c JOIN trips t ON t.id = c.trip_id WHERE c.session_id = ? ORDER BY t.name",
        (s["id"],)).fetchall()]
    actions = {r["action"]: r["n"] for r in db.execute(
        "SELECT action, COUNT(*) AS n FROM changes WHERE session_id = ? GROUP BY action", (s["id"],)).fetchall()}
    return {"id": s["id"], "name": s["name"], "ip": s["ip"], "user_agent": s["user_agent"], "started_at": s["started_at"],
            "last_at": s["last_at"], "open": audit.is_open(s), "changes": counts["n"] or 0,
            "pending": counts["pending"] or 0, "trips": trips, "actions": actions}


@adm.get("/sessions")
@require_admin
def sessions():
    db = get_db()
    return jsonify([_session_view(db, s) for s in rows(db.execute("SELECT * FROM sessions ORDER BY id DESC"))])


@adm.patch("/sessions/<int:sid>")
@require_admin
def rename_session(sid):
    db = get_db()
    s = row(db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)))
    if not s:
        abort(404, description="Sesión no encontrada")
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip() or None
    db.execute("UPDATE sessions SET name = ? WHERE id = ?", (name, sid))
    db.commit()
    return jsonify(_session_view(db, row(db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)))))


def _change_view(c):
    out = dict(c)
    out.pop("snapshot", None)
    out["has_snapshot"] = c.get("snapshot") is not None
    return out


@adm.get("/sessions/<int:sid>/changes")
@require_admin
def session_changes(sid):
    db = get_db()
    return jsonify([_change_view(c) for c in rows(db.execute(
        "SELECT * FROM changes WHERE session_id = ? ORDER BY id", (sid,)))])


@adm.get("/changes")
@require_admin
def search_changes():
    """?landmark=<text or id> — the history of one landmark across sessions."""
    db = get_db()
    q = (request.args.get("landmark") or "").strip()
    if not q:
        abort(400, description="Indica un hito")
    if q.isdigit():
        res = rows(db.execute("SELECT * FROM changes WHERE landmark_id = ? ORDER BY id DESC LIMIT 300", (int(q),)))
    else:
        res = rows(db.execute("SELECT * FROM changes WHERE landmark_name LIKE ? ORDER BY id DESC LIMIT 300",
                              (f"%{q}%",)))
    return jsonify([_change_view(c) for c in res])


# ---------------------------------------------------------------- revert

def _free_id(db, table, wanted):
    return wanted if row(db.execute(f"SELECT 1 AS x FROM {table} WHERE id = ?", (wanted,))) is None else None


def _insert_landmark(db, snap, keep_id=True):
    cols = ["trip_id", "name", "architect", "city", "address", "year", "notes", "lat", "lon", "geocode_status",
            "url_archdaily", "url_av", "url_image1", "url_image2", "status", "nearest_stop_id", "drive_minutes",
            "drive_km", "drive_source", "sort_order", "name_key", "wikidata_id", "wikipedia_url", "images_status",
            "links_status"]
    vals = [snap.get(c) for c in cols]
    if snap.get("nearest_stop_id") and row(db.execute("SELECT 1 AS x FROM route_stops WHERE id = ?", (snap["nearest_stop_id"],))) is None:
        vals[cols.index("nearest_stop_id")] = None
        vals[cols.index("drive_source")] = None
    new_id = _free_id(db, "landmarks", snap["id"]) if keep_id else None
    if new_id:
        db.execute(f"INSERT INTO landmarks (id, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})", [new_id] + vals)
    else:
        new_id = db.execute(f"INSERT INTO landmarks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals).lastrowid
    for im in snap.get("images", []):
        _insert_image(db, dict(im, landmark_id=new_id))
    return new_id


def _insert_image(db, im):
    cols = ["landmark_id", "kind", "url", "thumb", "title", "page_url", "source", "position"]
    vals = [im.get(c) for c in cols]
    if im.get("id") and _free_id(db, "landmark_images", im["id"]):
        db.execute(f"INSERT INTO landmark_images (id, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})", [im["id"]] + vals)
    else:
        db.execute(f"INSERT INTO landmark_images ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)


def _insert_day(db, snap):
    cols = ["trip_id", "date", "position", "stop_id", "title", "notes"]
    vals = [snap.get(c) for c in cols]
    if snap.get("stop_id") and row(db.execute("SELECT 1 AS x FROM route_stops WHERE id = ?", (snap["stop_id"],))) is None:
        vals[cols.index("stop_id")] = None
    new_id = _free_id(db, "trip_days", snap["id"])
    if new_id:
        db.execute(f"INSERT INTO trip_days (id, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})", [new_id] + vals)
    else:
        new_id = db.execute(f"INSERT INTO trip_days ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals).lastrowid
    for it in snap.get("items", []):
        if it["kind"] == "hito" and row(db.execute("SELECT 1 AS x FROM landmarks WHERE id = ?", (it["landmark_id"],))) is None:
            continue                      # el hito se borró después: no se puede recuperar esa línea
        _insert_item(db, dict(it, day_id=new_id))
    return new_id


def _insert_item(db, snap):
    cols = ["day_id", "position", "at_time", "kind", "landmark_id", "text"]
    vals = [snap.get(c) for c in cols]
    if snap.get("id") and _free_id(db, "day_items", snap["id"]):
        db.execute(f"INSERT INTO day_items (id, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})", [snap["id"]] + vals)
    else:
        db.execute(f"INSERT INTO day_items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)


def _restore_stops(db, trip_id, stops):
    db.execute("DELETE FROM route_stops WHERE trip_id = ?", (trip_id,))
    for s in stops:
        cols = ["trip_id", "position", "city", "country", "notes", "lat", "lon", "geocode_status"]
        vals = [trip_id] + [s.get(c) for c in cols[1:]]
        if s.get("id") and _free_id(db, "route_stops", s["id"]):
            db.execute(f"INSERT INTO route_stops (id, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})", [s["id"]] + vals)
        else:
            db.execute(f"INSERT INTO route_stops ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
    db.execute("UPDATE landmarks SET nearest_stop_id = NULL, drive_minutes = NULL, drive_km = NULL, drive_source = NULL "
               "WHERE trip_id = ?", (trip_id,))


def revert_change(db, c):
    """Undo one change; returns a message. Every undo is itself journaled (as a normal change
    of the same kind, tagged revert_of) so it can be undone again."""
    if not c["revertible"]:
        return "no reversible"
    if c["reverted_by"]:
        return "ya revertido"
    snap = json.loads(c["snapshot"]) if c["snapshot"] else None
    action, lm_id, trip_id = c["action"], c["landmark_id"], c["trip_id"]
    lm = row(db.execute("SELECT * FROM landmarks WHERE id = ?", (lm_id,))) if lm_id else None
    if action != "trip_delete" and trip_id and not row(db.execute("SELECT 1 AS x FROM trips WHERE id = ?", (trip_id,))):
        return "el viaje ya no existe"
    msg = "revertido"

    if action == "edit" and c["field"] == "coords":
        if not lm:
            return "el hito ya no existe"
        current = {"lat": lm["lat"], "lon": lm["lon"], "geocode_status": lm["geocode_status"]}
        db.execute("UPDATE landmarks SET lat = ?, lon = ?, geocode_status = ?, drive_source = NULL, "
                   "nearest_stop_id = NULL, drive_minutes = NULL, drive_km = NULL WHERE id = ?",
                   (snap["lat"], snap["lon"], snap["geocode_status"], lm_id))
        audit.log(db, "edit", trip_id, lm_id, lm["name"], "coords", old=audit.coords_text(**current),
                  new=audit.coords_text(**snap), snapshot=current, revert_of=c["id"])
    elif action in ("status", "edit"):
        if not lm:
            return "el hito ya no existe"
        field, old = c["field"], c["old_value"]
        sets, params = [f"{field} = ?"], [old]
        if field in ("name", "architect"):
            sets.append("name_key = ?")
            params.append(excel.landmark_key(old if field == "name" else lm["name"],
                                             old if field == "architect" else lm["architect"]))
        try:
            db.execute(f"UPDATE landmarks SET {', '.join(sets)} WHERE id = ?", (*params, lm_id))
        except sqlite3.IntegrityError:
            return "ya existe otro hito con ese nombre y arquitecto"
        audit.log(db, action, trip_id, lm_id, lm["name"], field, old=c["new_value"], new=old, revert_of=c["id"])
    elif action == "upload_update":
        if not lm:
            return "el hito ya no existe"
        before = {k: lm[k] for k in audit.UPLOAD_FIELDS}
        sets = ", ".join(f"{k} = ?" for k in audit.UPLOAD_FIELDS)
        try:
            db.execute(f"UPDATE landmarks SET {sets}, drive_source = NULL WHERE id = ?",
                       (*[snap.get(k) for k in audit.UPLOAD_FIELDS], lm_id))
        except sqlite3.IntegrityError:
            return "ya existe otro hito con ese nombre y arquitecto"
        audit.log(db, "upload_update", trip_id, lm_id, lm["name"], snapshot=before, revert_of=c["id"])
    elif action == "create_landmark":
        if not lm:
            return "el hito ya no existe"
        audit.log(db, "delete_landmark", trip_id, lm_id, lm["name"], snapshot=audit.landmark_snapshot(db, lm_id), revert_of=c["id"])
        db.execute("DELETE FROM landmarks WHERE id = ?", (lm_id,))
    elif action == "delete_landmark":
        if lm:
            return "el hito ya existe"
        new_id = _insert_landmark(db, snap)
        audit.log(db, "create_landmark", trip_id, new_id, snap["name"], revert_of=c["id"])
        msg = f"hito recuperado (id {new_id})"
    elif action == "clear_landmarks":
        ids = []
        for s in snap:
            if row(db.execute("SELECT 1 AS x FROM landmarks WHERE trip_id = ? AND name_key = ?", (s["trip_id"], s["name_key"]))):
                continue
            ids.append(_insert_landmark(db, s))
        audit.log(db, "bulk_create", trip_id, snapshot=ids, revert_of=c["id"])
        msg = f"{len(ids)} hitos recuperados"
    elif action == "bulk_create":
        snaps = [audit.landmark_snapshot(db, i) for i in snap if audit.landmark_snapshot(db, i)]
        for s in snaps:
            db.execute("DELETE FROM landmarks WHERE id = ?", (s["id"],))
        audit.log(db, "clear_landmarks", trip_id, snapshot=snaps, revert_of=c["id"])
        msg = f"{len(snaps)} hitos eliminados"
    elif action == "image_add":
        im = row(db.execute("SELECT * FROM landmark_images WHERE id = ?", (snap["id"],))) if snap else None
        if not im:
            return "la imagen ya no existe"
        db.execute("DELETE FROM landmark_images WHERE id = ?", (im["id"],))
        audit.log(db, "image_delete", trip_id, lm_id, c["landmark_name"], snapshot=im, revert_of=c["id"])
    elif action == "image_delete":
        if not lm:
            return "el hito ya no existe"
        _insert_image(db, snap)
        audit.log(db, "image_add", trip_id, lm_id, c["landmark_name"], snapshot=snap, revert_of=c["id"])
    elif action == "image_order":
        current = [(r["id"], r["position"]) for r in db.execute(
            "SELECT id, position FROM landmark_images WHERE landmark_id = ?", (lm_id,)).fetchall()]
        for iid, pos in snap:
            db.execute("UPDATE landmark_images SET position = ? WHERE id = ?", (pos, iid))
        audit.log(db, "image_order", trip_id, lm_id, c["landmark_name"], snapshot=current, revert_of=c["id"])
    elif action == "image_kind":
        db.execute("UPDATE landmark_images SET kind = ? WHERE id = ?", (c["old_value"], snap["id"]))
        audit.log(db, "image_kind", trip_id, lm_id, c["landmark_name"], old=c["new_value"], new=c["old_value"],
                  snapshot=snap, revert_of=c["id"])
    elif action == "day_create":
        day = row(db.execute("SELECT * FROM trip_days WHERE id = ?", (snap["id"],)))
        if not day:
            return "el día ya no existe"
        items = rows(db.execute("SELECT * FROM day_items WHERE day_id = ? ORDER BY position, id", (day["id"],)))
        audit.log(db, "day_delete", trip_id, landmark_name=c["landmark_name"], snapshot=dict(day, items=items),
                  revert_of=c["id"])
        db.execute("DELETE FROM trip_days WHERE id = ?", (day["id"],))
    elif action == "day_delete":
        if row(db.execute("SELECT 1 AS x FROM trip_days WHERE id = ?", (snap["id"],))):
            return "el día ya existe"
        new_id = _insert_day(db, snap)
        audit.log(db, "day_create", trip_id, landmark_name=c["landmark_name"], snapshot={"id": new_id}, revert_of=c["id"])
        msg = "día recuperado"
    elif action == "day_edit":
        day = row(db.execute("SELECT * FROM trip_days WHERE id = ?", (snap["day_id"],)))
        if not day:
            return "el día ya no existe"
        value = c["old_value"]
        if c["field"] == "stop_id":
            value = int(value) if value else None
            if value and not row(db.execute("SELECT 1 AS x FROM route_stops WHERE id = ?", (value,))):
                value = None
        db.execute(f"UPDATE trip_days SET {c['field']} = ? WHERE id = ?", (value, day["id"]))
        audit.log(db, "day_edit", trip_id, landmark_name=c["landmark_name"], field=c["field"],
                  old=c["new_value"], new=value, snapshot=snap, revert_of=c["id"])
    elif action == "item_add":
        it = row(db.execute("SELECT * FROM day_items WHERE id = ?", (snap["id"],)))
        if not it:
            return "el elemento ya no existe"
        db.execute("DELETE FROM day_items WHERE id = ?", (it["id"],))
        audit.log(db, "item_delete", trip_id, lm_id, c["landmark_name"], snapshot=it, revert_of=c["id"])
    elif action == "item_delete":
        if row(db.execute("SELECT 1 AS x FROM day_items WHERE id = ?", (snap["id"],))):
            return "el elemento ya existe"
        if not row(db.execute("SELECT 1 AS x FROM trip_days WHERE id = ?", (snap["day_id"],))):
            return "su día ya no existe"
        if snap["kind"] == "hito" and not row(db.execute("SELECT 1 AS x FROM landmarks WHERE id = ?", (snap["landmark_id"],))):
            return "el hito ya no existe"
        _insert_item(db, snap)
        audit.log(db, "item_add", trip_id, lm_id, c["landmark_name"], snapshot=snap, revert_of=c["id"])
    elif action == "item_edit":
        it = row(db.execute("SELECT * FROM day_items WHERE id = ?", (snap["item_id"],)))
        if not it:
            return "el elemento ya no existe"
        db.execute(f"UPDATE day_items SET {c['field']} = ? WHERE id = ?", (c["old_value"], it["id"]))
        audit.log(db, "item_edit", trip_id, lm_id, c["landmark_name"], field=c["field"],
                  old=c["new_value"], new=c["old_value"], snapshot=snap, revert_of=c["id"])
    elif action == "item_move":
        it = row(db.execute("SELECT * FROM day_items WHERE id = ?", (snap["item_id"],)))
        if not it:
            return "el elemento ya no existe"
        back = int(c["old_value"])
        if not row(db.execute("SELECT 1 AS x FROM trip_days WHERE id = ?", (back,))):
            return "su día anterior ya no existe"
        db.execute("UPDATE day_items SET day_id = ?, position = ? WHERE id = ?", (back, snap["position"], it["id"]))
        audit.log(db, "item_move", trip_id, lm_id, c["landmark_name"], field="day_id", old=c["new_value"], new=back,
                  snapshot={"item_id": it["id"], "position": it["position"]}, revert_of=c["id"])
    elif action == "item_order":
        ids = [i for i, _ in snap]
        if not ids:
            return "sin elementos"
        current = [[r["id"], r["position"]] for r in db.execute(
            "SELECT id, position FROM day_items WHERE day_id = (SELECT day_id FROM day_items WHERE id = ?)",
            (ids[0],)).fetchall()]
        if not current:
            return "el día ya no existe"
        for item_id, pos in snap:
            db.execute("UPDATE day_items SET position = ? WHERE id = ?", (pos, item_id))
        audit.log(db, "item_order", trip_id, landmark_name=c["landmark_name"], snapshot=current, revert_of=c["id"])
    elif action == "route_upload":
        current = rows(db.execute("SELECT * FROM route_stops WHERE trip_id = ? ORDER BY position", (trip_id,)))
        _restore_stops(db, trip_id, snap)
        audit.log(db, "route_upload", trip_id, snapshot=current, revert_of=c["id"])
        msg = "ruta anterior restaurada; los tiempos se recalculan"
    elif action == "trip_rename":
        db.execute("UPDATE trips SET name = ? WHERE id = ?", (c["old_value"], trip_id))
        audit.log(db, "trip_rename", trip_id, old=c["new_value"], new=c["old_value"], revert_of=c["id"])
    elif action == "trip_create":
        t = row(db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)))
        if not t:
            return "el viaje ya no existe"
        audit.log(db, "trip_delete", trip_id, old=t["name"], snapshot=audit.trip_snapshot(db, trip_id), revert_of=c["id"])
        db.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
    elif action == "trip_delete":
        if row(db.execute("SELECT 1 AS x FROM trips WHERE id = ?", (trip_id,))):
            return "el viaje ya existe"
        tid = _free_id(db, "trips", snap["trip"]["id"])
        if tid:
            db.execute("INSERT INTO trips (id, name, created_at) VALUES (?, ?, ?)", (tid, snap["trip"]["name"], snap["trip"]["created_at"]))
        else:
            tid = db.execute("INSERT INTO trips (name) VALUES (?)", (snap["trip"]["name"],)).lastrowid
        _restore_stops(db, tid, snap["stops"])
        for s in snap["landmarks"]:
            _insert_landmark(db, dict(s, trip_id=tid))
        for d in snap.get("days", []):
            _insert_day(db, dict(d, trip_id=tid))
        audit.log(db, "trip_create", tid, new=snap["trip"]["name"], revert_of=c["id"])
        msg = f"viaje recuperado (id {tid})"
    else:
        return "acción desconocida"

    db.execute("UPDATE changes SET reverted_by = (SELECT MAX(id) FROM changes) WHERE id = ?", (c["id"],))
    return msg


@adm.post("/changes/<int:cid>/revert")
@require_admin
def revert_one(cid):
    db = get_db()
    c = row(db.execute("SELECT * FROM changes WHERE id = ?", (cid,)))
    if not c:
        abort(404, description="Cambio no encontrado")
    msg = revert_change(db, c)
    db.commit()
    return jsonify({"result": msg, "change": _change_view(row(db.execute("SELECT * FROM changes WHERE id = ?", (cid,))))})


@adm.post("/sessions/<int:sid>/revert")
@require_admin
def revert_session(sid):
    """Undo everything the session did that is still in effect, newest first."""
    db = get_db()
    results = []
    for c in rows(db.execute("SELECT * FROM changes WHERE session_id = ? AND revertible = 1 AND reverted_by IS NULL "
                             "AND revert_of IS NULL ORDER BY id DESC", (sid,))):
        results.append({"id": c["id"], "action": c["action"], "landmark": c["landmark_name"], "result": revert_change(db, c)})
    db.commit()
    return jsonify({"reverted": sum(1 for r in results if r["result"] not in ("ya revertido", "no reversible")), "details": results})


# ---------------------------------------------------------------- backups

def _backup_dir():
    d = Path(current_app.config["DB_PATH"]).parent / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(name):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or "") or ".." in name:
        abort(400, description="Nombre de archivo no válido")
    return name


@adm.get("/backups")
@require_admin
def list_backups():
    files = []
    for p in sorted(_backup_dir().iterdir(), key=lambda p: p.name, reverse=True):
        if p.is_file() and not p.name.startswith("."):
            files.append({"name": p.name, "size": p.stat().st_size,
                          "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "kind": "db" if p.suffix == ".db" else p.suffix.lstrip(".")})
    return jsonify(files)


@adm.post("/backups")
@require_admin
def make_backup():
    """A manual snapshot now: database copy + standalone HTML of every trip."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = _backup_dir()
    src = sqlite3.connect(current_app.config["DB_PATH"])
    dst = sqlite3.connect(out / f"manual-{stamp}.db")
    src.backup(dst); dst.close(); src.close()
    db = get_db()
    from .routes import _trip_payload
    for t in rows(db.execute("SELECT * FROM trips")):
        html, _ = export.standalone_html(_trip_payload(db, t))
        (out / f"manual-{stamp}-viaje{t['id']}.html").write_text(html, encoding="utf-8")
    return jsonify({"created": f"manual-{stamp}.db"}), 201


@adm.get("/backups/<name>")
@require_admin
def download_backup(name):
    p = _backup_dir() / _safe_name(name)
    if not p.is_file():
        abort(404, description="Archivo no encontrado")
    return send_file(p, as_attachment=True, download_name=p.name)


@adm.get("/export.db")
@require_admin
def export_db():
    """The live database, as a consistent copy."""
    buf = io.BytesIO()
    tmp = sqlite3.connect(":memory:")
    src = sqlite3.connect(current_app.config["DB_PATH"]); src.backup(tmp); src.close()
    buf.write("\n".join(tmp.iterdump()).encode("utf-8"))   # SQL dump: portable and diffable
    tmp.close(); buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return send_file(buf, mimetype="application/sql", as_attachment=True, download_name=f"archtrip-{stamp}.sql")


@adm.post("/backups/<name>/restore")
@require_admin
def restore_backup(name):
    """Replace the live data with a stored .db. The current state is saved first as
    pre-restore-<stamp>.db, so a restore can itself be undone by restoring that file."""
    p = _backup_dir() / _safe_name(name)
    if not p.is_file() or p.suffix != ".db":
        abort(404, description="Copia .db no encontrada")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    live_path = current_app.config["DB_PATH"]
    live = sqlite3.connect(live_path)
    keep = sqlite3.connect(_backup_dir() / f"pre-restore-{stamp}.db"); live.backup(keep); keep.close()
    src = sqlite3.connect(p); src.backup(live); src.close(); live.close()
    db = get_db()
    from .db import init_db
    init_db()                                   # the copy may predate newer columns
    audit.log(db, "restore", old=None, new=name, snapshot={"saved_before": f"pre-restore-{stamp}.db"}, revertible=False)
    db.commit()
    return jsonify({"restored": name, "saved_before": f"pre-restore-{stamp}.db"})
