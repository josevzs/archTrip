"""El bot de Telegram del profesor: aprobar las fotos de los alumnos y enterarse de lo que pasa.

Cada foto que recoge inbox.py le llega al profesor con el nombre del alumno, la obra y tres
botones: Publicar · Rechazar · Cambiar obra. Publicar la deja aprobada (sale en la web de los
alumnos en la siguiente publicación); rechazar quita la imagen de la obra y sus copias, pero el
original se queda en data/fotos-alumnos/, que es el archivo; cambiar obra enseña las obras del día
en que se hizo la foto. Además avisa si el buzón se llena (5 GB), si la recogida lleva más de una
hora fallando, y manda un resumen al final del día.

Telegram se consulta con getUpdates (long polling), así que panda sigue sin recibir conexiones:
nada de webhooks. Sin `ARCHTRIP_TELEGRAM_TOKEN` y `ARCHTRIP_TELEGRAM_CHAT`, silencio. Lo que
decide qué se dice y qué hace cada botón está en funciones que no tocan la red: el transporte se
pasa como argumento y las pruebas usan uno de mentira."""
import io
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import inbox, uploads
from .db import get_db, row, rows

API = "https://api.telegram.org"
SUMMARY_HOUR = 22                     # el resumen del día, a esta hora (ARCHTRIP_TELEGRAM_HORA)
FAILING_ALARM = 3600                  # segundos de recogida fallando antes de avisar
MAX_CHOICES = 30                      # obras en «Cambiar obra», como mucho

log = logging.getLogger(__name__)
_started = False
# lo que ya se ha avisado, para no repetirlo; en memoria: tras un reinicio puede repetirse una vez
sent = {"full": False, "failing": None, "summary_day": None}


class TelegramError(Exception):
    pass


def configured():
    return bool(os.environ.get("ARCHTRIP_TELEGRAM_TOKEN") and os.environ.get("ARCHTRIP_TELEGRAM_CHAT"))


def chat_id():
    return os.environ.get("ARCHTRIP_TELEGRAM_CHAT", "").strip()


class Transport:
    """La API de Telegram. Las pruebas usan uno de mentira con el mismo `call`."""

    def __init__(self, token=None):
        self.token = token or os.environ.get("ARCHTRIP_TELEGRAM_TOKEN", "")

    def call(self, method, files=None, http_timeout=60, **params):
        url = f"{API}/bot{self.token}/{method}"
        try:
            if files:                     # multipart: lo que no es texto va como JSON
                data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
                r = requests.post(url, data=data, files=files, timeout=http_timeout)
            else:
                r = requests.post(url, json=params, timeout=http_timeout)
            body = r.json()
        except (requests.RequestException, ValueError) as exc:
            # la dirección lleva el token: que no acabe en el registro
            raise TelegramError(str(exc).replace(self.token, "***")) from None
        if not body.get("ok"):
            raise TelegramError(body.get("description") or f"Telegram responde {r.status_code}")
        return body.get("result")


# ------------------------------------------------------------ qué se dice (funciones puras)

STATE_LINE = {"publicada": "✓ Publicada", "rechazada": "✗ Rechazada"}


def caption(photo, landmark=None, day=None):
    """El texto de la foto: quién, qué obra y cuándo; y cómo ha acabado, si ya se decidió."""
    lines = []
    if photo["state"] in STATE_LINE:
        lines.append(STATE_LINE[photo["state"]])
    lines.append("📷 " + photo["author"])
    if landmark:
        lines.append(f"{landmark['name']} — {landmark['architect']}")
        lines.append(" · ".join(x for x in (landmark.get("city"), f"día {day}" if day else "") if x))
    else:
        lines.append(f"Sin obra: «{photo.get('landmark_name') or '?'}» ya no está en el viaje. Elige otra.")
    when = (photo.get("taken_at") or "").replace("T", " ")[:16]
    if when:
        lines.append("hecha el " + when)
    return "\n".join(line for line in lines if line)


def review_keyboard(sid, has_landmark=True):
    first = [{"text": "✓ Publicar", "callback_data": f"pub:{sid}"}] if has_landmark else []
    return {"inline_keyboard": [first + [{"text": "✗ Rechazar", "callback_data": f"rech:{sid}"}],
                                [{"text": "↔ Cambiar obra", "callback_data": f"obra:{sid}"}]]}


def choose_keyboard(sid, choices, current=None):
    """Una obra por fila (el texto de los botones de Telegram es corto) y la vuelta atrás."""
    rows_ = []
    for lm in choices[:MAX_CHOICES]:
        text = f"{lm['name']} — {lm['architect']}"
        text = (text[:56] + "…") if len(text) > 57 else text
        rows_.append([{"text": ("• " if lm["id"] == current else "") + text, "callback_data": f"set:{sid}:{lm['id']}"}])
    rows_.append([{"text": "« volver", "callback_data": f"back:{sid}"}])
    return {"inline_keyboard": rows_}


def parse_callback(data):
    """'pub:<id>' -> ('pub', '<id>', None); 'set:<id>:<obra>' -> ('set', '<id>', obra)."""
    parts = (data or "").split(":")
    if len(parts) == 2 and parts[0] in ("pub", "rech", "obra", "back") and parts[1]:
        return parts[0], parts[1], None
    if len(parts) == 3 and parts[0] == "set" and parts[1] and parts[2].isdigit():
        return "set", parts[1], int(parts[2])
    return None, None, None


def day_choices(days, landmarks, photo_date, current_id):
    """Las obras entre las que elegir: las del día en que se hizo la foto; si ese día no está en el
    itinerario, las de los días en que sale la obra actual; si tampoco, todas. `days` es la lista
    ordenada del itinerario y `landmarks`, {id: obra} con las que se pueden elegir."""
    def of(day_list):
        ids = []
        for d in day_list:
            for it in d.get("items") or []:
                if it["kind"] == "hito" and it.get("landmark_id") in landmarks and it["landmark_id"] not in ids:
                    ids.append(it["landmark_id"])
        return [landmarks[i] for i in ids]
    same = of([d for d in days if photo_date and d.get("date") == photo_date])
    if same:
        return same
    around = of([d for d in days if any(it.get("landmark_id") == current_id for it in d.get("items") or [])])
    return around or sorted(landmarks.values(), key=lambda lm: (lm["name"] or "").lower())


def summary_text(photos, authors, pending):
    if not photos and not pending:
        return None                                    # un día sin nada no merece mensaje
    s = f"Hoy: {photos} {'foto' if photos == 1 else 'fotos'} de {authors} {'alumno' if authors == 1 else 'alumnos'}"
    return s + (f", {pending} sin aprobar." if pending else ", todo aprobado.")


def alarms_due(now, health, status, done):
    """-> [(clave, texto)] de los avisos que tocan ahora y no se han dado. `done` es `sent`."""
    out = []
    if status and status.get("limite_bytes") and status.get("bytes", 0) >= status["limite_bytes"]:
        if not done["full"]:
            out.append(("full", f"⚠ El buzón ha llegado a {status['bytes'] / 1e9:.1f} GB y ya no admite fotos. "
                                "La recogida debería vaciarlo: mira si panda tiene red."))
    elif done["full"]:
        out.append(("full_ok", "✓ El buzón vuelve a tener sitio."))
    since = health.get("failing_since")
    if since and now - since >= FAILING_ALARM and done["failing"] != since:
        hours = (now - since) / 3600
        out.append(("failing", f"⚠ La recogida del buzón lleva {hours:.0f} h fallando: {health.get('last_error') or '?'}"))
    elif not since and done["failing"]:
        out.append(("failing_ok", "✓ La recogida del buzón vuelve a funcionar."))
    return out


def summary_due(local_now, last_day, hour=SUMMARY_HOUR):
    return local_now.hour >= hour and last_day != local_now.date().isoformat()


# ------------------------------------------------------------ lo que hace (con base y transporte)

def _trip_days(db, trip_id):
    from .routes import _trip_days as days
    return days(db, trip_id) if trip_id else []


def _day_of(days, landmark_id):
    for n, d in enumerate(days, start=1):
        if any(it.get("landmark_id") == landmark_id for it in d.get("items") or []):
            return n
    return None


def _render(db, photo):
    lm = row(db.execute("SELECT * FROM landmarks WHERE id = ?", (photo["landmark_id"],))) if photo["landmark_id"] else None
    return caption(photo, lm, _day_of(_trip_days(db, photo["trip_id"]), lm["id"]) if lm else None), lm


def _preview(db, photo):
    """Los bytes que se le mandan: la copia grande (Telegram no admite fotos de más de 10 MB y el
    original puede tener 25); sin copia, una hecha al vuelo del original."""
    if photo["image_id"]:
        im = row(db.execute("SELECT url FROM landmark_images WHERE id = ?", (photo["image_id"],)))
        data = uploads.read_file(im["url"]) if im else None
        if data:
            return data
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(photo["original"]))
        img = img.convert("RGB")
        img.thumbnail((1600, 1600))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception:                                  # noqa: BLE001 — sin vista previa, va el texto
        return None


def send_reviews(db, tg):
    """Manda al profesor cada foto recogida que aún no tiene mensaje. -> cuántas."""
    n = 0
    for photo in rows(db.execute("SELECT * FROM inbox_photos WHERE tg_message_id IS NULL ORDER BY collected_at, id")):
        if photo["state"] == "ilegible":
            msg = tg.call("sendMessage", chat_id=chat_id(),
                          text=f"⚠ Llegó una foto de {photo['author']} ({photo.get('landmark_name') or '?'}) que no se "
                               f"puede abrir. El original está guardado: {photo['original']}")
        else:
            text, lm = _render(db, photo)
            keyboard = review_keyboard(photo["id"], has_landmark=lm is not None)
            image = _preview(db, photo)
            if image:
                msg = tg.call("sendPhoto", chat_id=chat_id(), caption=text, reply_markup=keyboard,
                              files={"photo": (f"{photo['id']}.jpg", image, "image/jpeg")})
            else:
                msg = tg.call("sendMessage", chat_id=chat_id(), text=text, reply_markup=keyboard)
        db.execute("UPDATE inbox_photos SET tg_message_id = ? WHERE id = ?", (msg["message_id"], photo["id"]))
        db.commit()
        n += 1
    return n


def _edit(tg, message, text, keyboard=None):
    kw = {"chat_id": message["chat"]["id"], "message_id": message["message_id"]}
    if keyboard is not None:
        kw["reply_markup"] = keyboard
    if "photo" in message:
        tg.call("editMessageCaption", caption=text, **kw)
    else:
        tg.call("editMessageText", text=text, **kw)


def on_callback(db, tg, cb):
    """Lo que hace cada botón. Solo atiende al chat del profesor."""
    message = cb.get("message") or {}
    if str((message.get("chat") or {}).get("id")) != chat_id():
        return None
    action, sid, arg = parse_callback(cb.get("data"))
    photo = row(db.execute("SELECT * FROM inbox_photos WHERE id = ?", (sid,))) if sid else None
    answer = lambda text: tg.call("answerCallbackQuery", callback_query_id=cb["id"], text=text)  # noqa: E731
    if not photo:
        return answer("Esa foto ya no está.")
    if photo["state"] in ("publicada", "rechazada") and action != "back":
        return answer("Ya estaba " + photo["state"] + ".")
    now = inbox.now_iso()

    if action == "pub":
        if not photo["image_id"]:
            return answer("Primero elige la obra.")
        db.execute("UPDATE landmark_images SET approved = 1 WHERE id = ?", (photo["image_id"],))
        db.execute("UPDATE inbox_photos SET state = 'publicada', decided_at = ? WHERE id = ?", (now, sid))
        db.commit()
        photo["state"] = "publicada"
        _edit(tg, message, _render(db, photo)[0], {"inline_keyboard": []})
        return answer("Publicada: sale en la web en la próxima actualización.")

    if action == "rech":
        if photo["image_id"]:                          # la imagen y sus copias se van; el original, no
            im = row(db.execute("SELECT * FROM landmark_images WHERE id = ?", (photo["image_id"],)))
            if im:
                uploads.delete_files(im)
                db.execute("DELETE FROM landmark_images WHERE id = ?", (im["id"],))
        db.execute("UPDATE inbox_photos SET state = 'rechazada', image_id = NULL, decided_at = ? WHERE id = ?", (now, sid))
        db.commit()
        photo["state"] = "rechazada"
        _edit(tg, message, _render(db, photo)[0], {"inline_keyboard": []})
        return answer("Rechazada. El original queda guardado.")

    if action in ("obra", "set", "back"):
        trip_id = photo["trip_id"]
        lms = {lm["id"]: lm for lm in rows(db.execute(
            "SELECT * FROM landmarks WHERE trip_id = ? AND status != 'descartado' ORDER BY sort_order, id", (trip_id,)))}
        if action == "obra":
            date = (photo.get("taken_at") or photo.get("sent_at") or "")[:10]
            choices = day_choices(_trip_days(db, trip_id), lms, date, photo["landmark_id"])
            _edit(tg, message, _render(db, photo)[0], choose_keyboard(sid, choices, photo["landmark_id"]))
            return answer("¿Qué obra sale en la foto?")
        if action == "set":
            lm = lms.get(arg)
            if not lm:
                return answer("Esa obra ya no está en el viaje.")
            if photo["image_id"]:
                pos = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM landmark_images WHERE landmark_id = ?",
                                 (lm["id"],)).fetchone()[0]
                db.execute("UPDATE landmark_images SET landmark_id = ?, position = ? WHERE id = ?", (lm["id"], pos, photo["image_id"]))
                image_id = photo["image_id"]
            else:                                     # una foto que llegó sin obra: ahora sí se hacen las copias
                try:
                    url, thumb = uploads.store_file(lm["id"], Path(photo["original"]).read_bytes())
                except (OSError, ValueError):
                    return answer("No se puede abrir el original.")
                pos = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM landmark_images WHERE landmark_id = ?",
                                 (lm["id"],)).fetchone()[0]
                image_id = db.execute(
                    "INSERT INTO landmark_images (landmark_id, kind, url, thumb, title, page_url, source, position, "
                    "author, submission_id, approved) VALUES (?, 'foto', ?, ?, ?, NULL, 'alumno', ?, ?, ?, 0)",
                    (lm["id"], url, thumb, f"Foto de {photo['author']}", pos, photo["author"], sid)).lastrowid
            db.execute("UPDATE inbox_photos SET landmark_id = ?, landmark_name = ?, image_id = ?, state = 'pendiente' WHERE id = ?",
                       (lm["id"], lm["name"], image_id, sid))
            db.commit()
            photo.update(landmark_id=lm["id"], image_id=image_id, state="pendiente")
        text, lm = _render(db, photo)
        _edit(tg, message, text, review_keyboard(sid, has_landmark=lm is not None))
        return answer(f"Ahora es de «{lm['name']}»." if action == "set" and lm else "")
    return answer("")


def housekeeping(db, tg, now=None, local_now=None):
    """Lo que el bot dice por su cuenta: fotos nuevas, avisos y el resumen del día."""
    now = time.time() if now is None else now
    send_reviews(db, tg)
    for key, text in alarms_due(now, inbox.health, inbox.health.get("last_status"), sent):
        tg.call("sendMessage", chat_id=chat_id(), text=text)
        if key == "full":
            sent["full"] = True
        elif key == "full_ok":
            sent["full"] = False
        elif key == "failing":
            sent["failing"] = inbox.health["failing_since"]
        elif key == "failing_ok":
            sent["failing"] = None
    local_now = local_now or _local_now()
    hour = int(os.environ.get("ARCHTRIP_TELEGRAM_HORA", SUMMARY_HOUR))
    if summary_due(local_now, sent["summary_day"], hour):
        today = local_now.date().isoformat()
        offset = local_now.utcoffset() or timedelta(0)
        day_rows = [r for r in rows(db.execute("SELECT author, collected_at FROM inbox_photos"))
                    if _to_local(r["collected_at"], offset) == today]
        pending = db.execute("SELECT COUNT(*) FROM inbox_photos WHERE state IN ('pendiente', 'sin_obra')").fetchone()[0]
        text = summary_text(len(day_rows), len({r["author"].strip().lower() for r in day_rows}), pending)
        if text:
            tg.call("sendMessage", chat_id=chat_id(), text=text)
        sent["summary_day"] = today


def _to_local(iso, offset):
    try:
        return (datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ") + offset).date().isoformat()
    except (TypeError, ValueError):
        return None


def _local_now():
    """La hora del resumen, en `ARCHTRIP_TELEGRAM_TZ` (p. ej. Asia/Tokyo) o, si no, la del servidor."""
    name = os.environ.get("ARCHTRIP_TELEGRAM_TZ")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(name))
        except Exception:                             # noqa: BLE001 — zona desconocida: la del servidor
            log.warning("telegram: zona horaria desconocida %r; uso la del servidor", name)
    return datetime.now(timezone.utc).astimezone()


def poll_once(db, tg, offset=None, timeout=25):
    """Una espera larga a Telegram y lo que haya llegado. -> el offset siguiente."""
    params = {"timeout": timeout, "allowed_updates": ["callback_query"]}
    if offset is not None:
        params["offset"] = offset
    for update in tg.call("getUpdates", http_timeout=timeout + 10, **params) or []:
        offset = update["update_id"] + 1
        if "callback_query" in update:
            try:
                on_callback(db, tg, update["callback_query"])
            except Exception:                         # noqa: BLE001 — un botón roto no para el bot
                db.rollback()
                log.warning("telegram: no se ha podido atender un botón", exc_info=True)
    return offset


def start_worker(app):
    global _started
    if _started or app.config.get("TESTING") or os.environ.get("ARCHTRIP_TELEGRAM_WORKER") == "0" or not configured():
        return
    _started = True

    def loop():
        tg, offset = Transport(), None
        while True:
            try:
                with app.app_context():
                    db = get_db()
                    housekeeping(db, tg)
                    offset = poll_once(db, tg, offset)
            except Exception as exc:                  # noqa: BLE001 — sin red, se espera y se vuelve
                app.logger.warning("telegram: %s", exc)
                time.sleep(30)

    threading.Thread(target=loop, name="archtrip-telegram", daemon=True).start()
