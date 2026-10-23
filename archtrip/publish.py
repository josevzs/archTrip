"""Publicar la versión de consulta de un viaje (la de los alumnos) en Cloudflare Pages.

Desde la administración se elige el viaje y el nombre de la web, y un hilo de fondo hace el resto:
la primera subida y, mientras «actualizar sola» siga marcada, una nueva cada vez que el viaje
cambie (lo mira cada `ARCHTRIP_PUBLISH_EVERY` segundos, 600 de serie). Lo que se sube es
la copia HTML con `alumnos=True` (ver `export.student_payload`) y los dos PDF del itinerario.

Las claves van en variables de entorno, nunca en la base de datos: `CLOUDFLARE_ACCOUNT_ID` y
`CLOUDFLARE_API_TOKEN` (un token con permiso «Cloudflare Pages: Edit»). El proyecto se crea y se
borra con la API de Cloudflare; los archivos se suben con `wrangler`, que va dentro de la imagen
de Docker (`ARCHTRIP_WRANGLER` cambia cómo se le llama). Un solo hilo por proceso: la imagen
arranca gunicorn con un único worker, igual que necesita el limitador de Nominatim."""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import export
from .db import get_db, row, rows

CF_API = "https://api.cloudflare.com/client/v4"
MAX_FILE = 25 * 1024 * 1024          # Cloudflare Pages no acepta archivos más grandes
PROJECT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,56}[a-z0-9])?$")

_wake = threading.Event()
_started = False


class PublishError(Exception):
    pass


def configured():
    return bool(os.environ.get("CLOUDFLARE_ACCOUNT_ID") and os.environ.get("CLOUDFLARE_API_TOKEN"))


def wrangler_cmd():
    custom = os.environ.get("ARCHTRIP_WRANGLER")
    if custom:
        return shlex.split(custom)
    return ["wrangler"] if shutil.which("wrangler") else ["npx", "--yes", "wrangler@4"]


def project_name(text):
    """Un nombre válido para Cloudflare a partir del del viaje: «Tohoku (variante)» → tohoku-variante."""
    return export.slugify(text)[:58].strip("-") or "viaje"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------ la cola

def listing(db):
    """Todos los viajes, cada uno con su publicación si la tiene."""
    out = []
    for t in rows(db.execute("""
            SELECT t.id AS trip_id, t.name AS trip_name, t.private, p.project, p.url, p.auto, p.state,
                   p.requested, p.published_at, p.error
            FROM trips t LEFT JOIN publications p ON p.trip_id = t.id
            ORDER BY (p.trip_id IS NULL), t.created_at DESC, t.id DESC""")):
        t["suggested"] = project_name(t["trip_name"])
        out.append(t)
    return out


def request_publish(db, trip_id, project=None):
    """Da de alta (o vuelve a pedir) la publicación de un viaje; el hilo la sube enseguida."""
    trip = row(db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)))
    if trip is None:
        raise PublishError("Viaje no encontrado")
    if trip["private"]:
        raise PublishError("Este viaje está «solo en local»: publícalo en la lista de viajes antes de sacarlo a la web")
    pub = row(db.execute("SELECT * FROM publications WHERE trip_id = ?", (trip_id,)))
    if pub is None:
        project = (project or project_name(trip["name"])).strip().lower()
        if not PROJECT_RE.match(project):
            raise PublishError("El nombre de la web solo puede llevar minúsculas, números y guiones (hasta 58)")
        if db.execute("SELECT 1 FROM publications WHERE project = ?", (project,)).fetchone():
            raise PublishError(f"Ya hay otro viaje publicado como «{project}»")
        db.execute("INSERT INTO publications (trip_id, project, requested) VALUES (?, ?, 1)", (trip_id, project))
    else:
        db.execute("UPDATE publications SET requested = 1 WHERE trip_id = ?", (trip_id,))
    db.commit()
    _wake.set()


def set_auto(db, trip_id, auto):
    db.execute("UPDATE publications SET auto = ? WHERE trip_id = ?", (1 if auto else 0, trip_id))
    db.commit()


def withdraw(db, trip_id):
    """Borra la web de Cloudflare y deja de publicar el viaje."""
    pub = row(db.execute("SELECT * FROM publications WHERE trip_id = ?", (trip_id,)))
    if pub is None:
        raise PublishError("Este viaje no está publicado")
    if pub["url"]:                       # solo existe en Cloudflare si llegó a crearse
        cloudflare_delete(pub["project"])
    db.execute("DELETE FROM publications WHERE trip_id = ?", (trip_id,))
    db.commit()


# ------------------------------------------------------------ el trabajo

def snapshot(db, trip):
    """-> (payload, alumnos, hash). El hash es de lo que se publica y de la app, no de la fecha de
    exportación: si nada de eso ha cambiado sale el mismo y no hay nada que subir. Es barato; los
    PDF solo se hacen cuando toca subir."""
    from .routes import _trip_payload
    payload = _trip_payload(db, trip)
    students = export.student_payload(payload)
    template = (export.STATIC_DIR / "index.html").read_bytes()
    digest = hashlib.sha256(template + json.dumps(students, sort_keys=True, default=str).encode()).hexdigest()
    return payload, students, digest


def build_files(payload, students):
    html, _ = export.standalone_html(payload, alumnos=True, pdfs=True)
    files = {
        "index.html": html.encode("utf-8"),
        "itinerario.pdf": export.itinerary_pdf(students)[0],
        "itinerario-fotos.pdf": export.itinerary_pdf(students, gallery=True)[0],
        # que los buscadores no lo indexen (la página ya lo pide; esto cubre también los PDF)
        "_headers": b"/*\n  X-Robots-Tag: noindex, nofollow\n",
    }
    big = [name for name, data in files.items() if len(data) > MAX_FILE]
    if big:
        raise PublishError(f"{', '.join(big)} pasa de 25 MiB y Cloudflare no lo admite")
    return files


def publish_one(db, pub, force=False):
    """Sube un viaje si ha cambiado (o si se ha pedido a mano). Devuelve True si ha subido algo."""
    trip = row(db.execute("SELECT * FROM trips WHERE id = ?", (pub["trip_id"],)))
    if trip is None or trip["private"]:
        raise PublishError("El viaje ha pasado a «solo en local»: no se actualiza la web")
    payload, students, digest = snapshot(db, trip)
    if not force and digest == pub["hash"]:
        return False
    db.execute("UPDATE publications SET state = 'publicando', error = NULL WHERE trip_id = ?", (pub["trip_id"],))
    db.commit()
    files = build_files(payload, students)
    url = pub["url"]
    if not url:                          # se guarda ya: si la subida falla, «retirar» lo encuentra
        url = cloudflare_create(pub["project"])
        db.execute("UPDATE publications SET url = ? WHERE trip_id = ?", (url, pub["trip_id"]))
        db.commit()
    deploy(pub["project"], files)
    db.execute("UPDATE publications SET state = 'ok', hash = ?, url = ?, published_at = ?, error = NULL "
               "WHERE trip_id = ?", (digest, url, now_iso(), pub["trip_id"]))
    db.commit()
    return True


def run_once(db):
    """Una pasada de la cola: lo pedido a mano primero, luego lo que se actualiza solo."""
    pubs = rows(db.execute("SELECT * FROM publications WHERE requested = 1 OR auto = 1 ORDER BY requested DESC"))
    done = 0
    for pub in pubs:
        forced = bool(pub["requested"])
        # lo pedido a mano se ve «subiendo…» desde ya: preparar los PDF ya lleva su rato
        db.execute("UPDATE publications SET requested = 0, state = CASE WHEN ? THEN 'publicando' ELSE state END "
                   "WHERE trip_id = ?", (forced, pub["trip_id"]))
        db.commit()
        if not configured():
            _fail(db, pub, "Faltan CLOUDFLARE_ACCOUNT_ID y CLOUDFLARE_API_TOKEN en el servidor")
            continue
        try:
            done += publish_one(db, pub, force=forced)
        except Exception as exc:          # noqa: BLE001 — cualquier fallo queda a la vista en admin
            _fail(db, pub, str(exc) or exc.__class__.__name__)
    return done


def _fail(db, pub, message):
    db.execute("UPDATE publications SET state = 'error', error = ? WHERE trip_id = ?", (message[-1500:], pub["trip_id"]))
    db.commit()


def start_worker(app):
    """El hilo de fondo. Se despierta cuando se pide una publicación o cada pocos minutos."""
    global _started
    if _started or app.config.get("TESTING") or os.environ.get("ARCHTRIP_PUBLISH_WORKER") == "0":
        return
    _started = True
    every = float(os.environ.get("ARCHTRIP_PUBLISH_EVERY", "600"))

    def loop():
        with app.app_context():          # una subida cortada por un reinicio se vuelve a intentar
            db = get_db()
            db.execute("UPDATE publications SET state = 'pendiente', requested = 1 WHERE state = 'publicando'")
            db.commit()
        while True:
            _wake.wait(every)
            _wake.clear()
            try:
                with app.app_context():
                    run_once(get_db())
            except Exception:             # noqa: BLE001 — el hilo no se puede morir
                app.logger.exception("publicación")

    threading.Thread(target=loop, name="archtrip-publish", daemon=True).start()
    _wake.set()                           # una primera pasada al arrancar


# ------------------------------------------------------------ Cloudflare

def _cf(method, path, **kw):
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    try:
        r = requests.request(method, f"{CF_API}/accounts/{account}{path}", timeout=60,
                             headers={"Authorization": f"Bearer {token}"}, **kw)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise PublishError(f"No se ha podido hablar con Cloudflare: {exc}") from exc
    return r.status_code, data


def _cf_error(data):
    return "; ".join(e.get("message", "") for e in data.get("errors") or []) or "error desconocido"


def cloudflare_create(project):
    """Crea el proyecto (o reutiliza el que ya exista con ese nombre) -> su dirección pública.
    Si el nombre ya lo usa otra cuenta, Cloudflare le añade un sufijo: la dirección buena es la
    que devuelve, no la que se pidió."""
    status, data = _cf("POST", "/pages/projects", json={"name": project, "production_branch": "main"})
    if not data.get("success"):
        status, data = _cf("GET", f"/pages/projects/{project}")
        if not data.get("success"):
            raise PublishError("Cloudflare no deja crear el proyecto: " + _cf_error(data))
    return "https://" + (data["result"].get("subdomain") or f"{project}.pages.dev")


def cloudflare_delete(project):
    status, data = _cf("DELETE", f"/pages/projects/{project}")
    if not data.get("success") and status != 404:
        raise PublishError("Cloudflare no deja borrar el proyecto: " + _cf_error(data))


def deploy(project, files):
    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp) / "site"
        site.mkdir()
        for name, data in files.items():
            (site / name).write_bytes(data)
        env = dict(os.environ, WRANGLER_SEND_METRICS="false", CI="true")
        try:
            out = subprocess.run(wrangler_cmd() + ["pages", "deploy", "site", "--project-name", project,
                                                   "--branch", "main", "--commit-dirty=true"],
                                 cwd=tmp, env=env, capture_output=True, text=True, timeout=900)
        except FileNotFoundError as exc:
            raise PublishError("No está wrangler (ni npx) en el servidor: hace falta Node.js 22") from exc
        except subprocess.TimeoutExpired as exc:
            raise PublishError("La subida a Cloudflare ha tardado más de 15 minutos") from exc
        if out.returncode != 0:
            raise PublishError("Falló la subida: " + (out.stderr or out.stdout).strip()[-1200:])
