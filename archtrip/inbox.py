"""Recoger las fotos de los alumnos del buzón (el Worker de buzon/) y traerlas al archivo.

El buzón es desechable y el archivo es esto: el SQLite y el disco de panda, que es lo que se
respalda. Panda no recibe conexiones; un hilo de fondo sale cada minuto (`ARCHTRIP_INBOX_EVERY`)
a ver qué hay, y en cada pasada:

1. deja en el buzón la lista de obras de cada viaje publicado (solo si ha cambiado), que es lo
   que el alumno ve en el formulario;
2. por cada envío nuevo trae los bytes, guarda el original intacto en `data/fotos-alumnos/` (no se
   vuelve a tocar: es el archivo), hace las dos copias de siempre (uploads.store_file) y lo apunta
   en `landmark_images` firmado y sin aprobar, y en `inbox_photos`;
3. avisa al buzón de lo que ya tiene, y solo entonces el buzón lo borra.

Si algo falla a mitad, ese envío no se da por recogido y se vuelve a intentar en la siguiente
pasada: mejor repetir una foto que perderla. Los duplicados se evitan con el id del envío. Una foto
cuya obra ya no existe, o que no se puede abrir, se guarda igual (original y registro) y sí se da
por recogida, para que no se quede en el cubo para siempre; queda a la vista como `sin_obra` o
`ilegible`.

Sin `ARCHTRIP_INBOX_URL` y `ARCHTRIP_INBOX_SECRET` no arranca. Un solo hilo por proceso, igual
que publish.py (gunicorn corre con un único worker)."""
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from flask import current_app

from . import export, uploads
from .db import get_db, row, rows

EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

_wake = threading.Event()
_started = False
_catalog_sent = {}                    # proyecto -> hash del último catálogo que aceptó el buzón
log = logging.getLogger(__name__)

# Cómo va la recogida, para los avisos (telegram.py): cuándo fue bien por última vez y desde cuándo
# falla. En memoria: tras un reinicio se empieza de cero, que es lo que interesa avisar.
health = {"last_ok": None, "failing_since": None, "last_error": None, "last_status": None}


class InboxError(Exception):
    pass


def configured():
    return bool(os.environ.get("ARCHTRIP_INBOX_URL") and os.environ.get("ARCHTRIP_INBOX_SECRET"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def originals_dir():
    return Path(current_app.config["DB_PATH"]).parent / "fotos-alumnos"


class Client:
    """Las rutas /panda/ del buzón. Las pruebas lo sustituyen por un doble con los mismos métodos."""

    def __init__(self, url=None, secret=None):
        self.url = (url or os.environ.get("ARCHTRIP_INBOX_URL", "")).rstrip("/")
        self.headers = {"Authorization": "Bearer " + (secret or os.environ.get("ARCHTRIP_INBOX_SECRET", ""))}

    def _call(self, method, path, timeout=60, headers=None, **kw):
        try:
            r = requests.request(method, self.url + path, headers={**self.headers, **(headers or {})},
                                 timeout=timeout, **kw)
        except requests.RequestException as exc:
            raise InboxError(f"El buzón no contesta: {exc}") from exc
        if r.status_code != 200:
            raise InboxError(f"El buzón responde {r.status_code} a {path}: {r.text[:200]}")
        return r

    def pending(self):
        return self._call("GET", "/panda/pendientes").json()["items"]

    def fetch(self, key):
        return self._call("GET", "/panda/objeto/" + quote(key, safe="/"), timeout=180).content

    def collected(self, ids):
        return self._call("POST", "/panda/recogido", json={"ids": list(ids)}).json()

    def put_catalog(self, trip, data):
        return self._call("PUT", "/panda/catalogo/" + quote(trip), data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json"}).json()

    def status(self):
        return self._call("GET", "/panda/estado").json()


# ------------------------------------------------------------ el catálogo

def catalog_for(db, trip):
    """Las obras que puede elegir un alumno: las mismas que ve en la web publicada (el payload de
    alumnos), con su sitio y el primer día del itinerario en que salen."""
    from .routes import _trip_payload
    students = export.student_payload(_trip_payload(db, trip))
    first_day = {}
    for n, day in enumerate(students["days"], start=1):
        for it in day.get("items") or []:
            if it["kind"] == "hito":
                first_day.setdefault(it.get("landmark_id"), n)
    return {"trip_name": trip["name"], "landmarks": [
        {"id": lm["id"], "name": lm["name"], "architect": lm["architect"], "city": lm["city"],
         "lat": lm.get("lat"), "lon": lm.get("lon"), "day": first_day.get(lm["id"])}
        for lm in students["landmarks"]]}


def published_trips(db):
    """Proyecto publicado -> viaje. El buzón conoce los viajes por el nombre de su web."""
    return {p["project"]: p for p in rows(db.execute(
        "SELECT p.project, t.* FROM publications p JOIN trips t ON t.id = p.trip_id WHERE t.private = 0"))}


def sync_catalogs(db, client, trips):
    for project, trip in trips.items():
        data = catalog_for(db, trip)
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()
        if _catalog_sent.get(project) != digest:
            client.put_catalog(project, data)
            _catalog_sent[project] = digest


# ------------------------------------------------------------ la recogida

def collect_one(db, client, sub, trips):
    """Trae un envío al archivo. Devuelve True si ya se puede dar por recogido (también si ya lo
    estaba); lanza si algo ha fallado y hay que reintentarlo."""
    if db.execute("SELECT 1 FROM inbox_photos WHERE id = ?", (sub["id"],)).fetchone():
        return True                                     # ya está: el aviso de recogido se perdió
    data = client.fetch(sub["object_key"])
    if sub.get("bytes") is not None and len(data) != sub["bytes"]:
        raise InboxError(f"el envío {sub['id']} llegó con {len(data)} bytes en vez de {sub['bytes']}")
    ext = EXT.get(sub.get("mime")) or sub["object_key"].rsplit(".", 1)[-1].lower()
    folder = originals_dir()
    folder.mkdir(parents=True, exist_ok=True)
    original = folder / f"{sub['id']}.{ext}"
    if not original.exists():                           # se escribe aparte y se renombra: o entero o nada
        tmp = original.with_suffix(original.suffix + ".parcial")
        tmp.write_bytes(data)
        os.replace(tmp, original)

    trip = trips.get(sub.get("trip"))
    lm = row(db.execute("SELECT * FROM landmarks WHERE id = ?", (sub["landmark_id"],)))
    if lm and trip and lm["trip_id"] != trip["id"]:
        lm = None                                       # ese número es de otro viaje: no se adivina
    state, image_id = "pendiente", None
    if lm is None:
        state = "sin_obra"
    else:
        try:
            url, thumb = uploads.store_file(lm["id"], data)
        except ValueError:
            state = "ilegible"
        else:
            pos = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM landmark_images WHERE landmark_id = ?",
                             (lm["id"],)).fetchone()[0]          # al final: no quita la foto principal
            image_id = db.execute(
                "INSERT INTO landmark_images (landmark_id, kind, url, thumb, title, page_url, source, position, "
                "author, submission_id, approved) VALUES (?, 'foto', ?, ?, ?, NULL, 'alumno', ?, ?, ?, 0)",
                (lm["id"], url, thumb, f"Foto de {sub['author']}", pos, sub["author"], sub["id"])).lastrowid
    db.execute(
        "INSERT INTO inbox_photos (id, trip_id, landmark_id, landmark_name, image_id, author, original, bytes, mime, "
        "taken_at, lat, lon, sent_at, collected_at, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sub["id"], (lm or {}).get("trip_id") or (trip or {}).get("id"), (lm or {}).get("id"), sub.get("landmark_name"),
         image_id, sub["author"], str(original), len(data), sub.get("mime"), sub.get("taken_at"), sub.get("lat"),
         sub.get("lon"), sub.get("created_at"), now_iso(), state))
    db.commit()
    return True


def run_once(db, client=None):
    """Una pasada: catálogos, recogida y aviso de lo recogido. -> lista de ids recogidos."""
    client = client or Client()
    try:
        trips = published_trips(db)
        sync_catalogs(db, client, trips)
        subs = client.pending()
    except Exception as exc:
        _failed(exc)
        raise
    done = []
    for sub in subs:
        try:
            if collect_one(db, client, sub, trips):
                done.append(sub["id"])
        except Exception as exc:                      # noqa: BLE001 — ese envío se reintenta luego
            db.rollback()
            log.warning("buzón: no se ha podido recoger %s: %s", sub.get("id"), exc)
            health["last_error"] = str(exc)
    if done:
        try:
            client.collected(done)
        except Exception as exc:
            _failed(exc)                              # el archivo ya los tiene; el buzón los repetirá
            raise
    health["last_ok"], health["failing_since"] = time.time(), None
    try:
        health["last_status"] = client.status()
    except Exception:                                 # noqa: BLE001 — el estado es solo para avisos
        pass
    return done


def _failed(exc):
    health["last_error"] = str(exc)
    if health["failing_since"] is None:
        health["failing_since"] = time.time()


def start_worker(app):
    """El hilo de fondo: una pasada cada `ARCHTRIP_INBOX_EVERY` segundos (60 de serie)."""
    global _started
    if _started or app.config.get("TESTING") or os.environ.get("ARCHTRIP_INBOX_WORKER") == "0" or not configured():
        return
    _started = True
    every = float(os.environ.get("ARCHTRIP_INBOX_EVERY", "60"))

    def loop():
        while True:
            try:
                with app.app_context():
                    run_once(get_db())
            except Exception:                         # noqa: BLE001 — el hilo no se puede morir
                app.logger.warning("buzón: la pasada ha fallado", exc_info=True)
            _wake.wait(every)
            _wake.clear()

    threading.Thread(target=loop, name="archtrip-inbox", daemon=True).start()
