"""Exports: a standalone single-file HTML copy, and an Obsidian Bases vault folder as ZIP."""
import base64
import io
import json
import re
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DATA_MARKER = "<!--ARCHTRIP_DATA-->"

STATUS_LABEL = {"pendiente": "Pendiente", "curado": "Curado", "posible": "Posible", "descartado": "Descartado"}


def slugify(text, fallback="viaje"):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or fallback


def safe_filename(text, fallback="sin-nombre"):
    """Keep accents (Obsidian is fine with them) but drop what filesystems reject."""
    text = re.sub(r'[\\/:*?"<>|]+', " ", text or "")
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    return text[:120] or fallback


# ------------------------------------------------------------ standalone

def standalone_html(payload):
    """-> (html, filename). Embeds the trip data into static/index.html so the
    file works from file:// with no server; the page persists edits in
    localStorage and can re-export itself ("Guardar copia")."""
    template = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    data = dict(payload)
    data["exported_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _inline_uploads(data["landmarks"])
    # never let the JSON close the script tag early
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    script = f'<script id="archtrip-data">window.__ARCHTRIP__ = {blob};</script>'
    if DATA_MARKER not in template:
        raise RuntimeError("static/index.html no contiene el marcador ARCHTRIP_DATA")
    html = template.replace(DATA_MARKER, script, 1)
    return html, f"viaje-{slugify(payload['trip']['name'])}.html"


def _inline_uploads(landmarks):
    """Photos uploaded by hand are served from this server; a standalone copy must carry them."""
    from . import uploads
    for lm in landmarks:
        for im in lm.get("images", []):
            for key in ("url", "thumb"):
                raw = uploads.read_file(im.get(key) or "")
                if raw is not None:
                    im[key] = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


# -------------------------------------------------------------- obsidian

def _yaml_value(v):
    if v is None or v == "":
        return '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{s}"'


def _frontmatter(fields):
    lines = ["---"]
    for k, v in fields:
        if v is None or v == "":
            continue
        lines.append(f"{k}: {_yaml_value(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _stop_note(stop):
    fm = _frontmatter([
        ("tipo", "parada"),
        ("orden", stop["position"]),
        ("ciudad", stop["city"]),
        ("pais", stop.get("country")),
        ("lat", stop.get("lat")),
        ("lon", stop.get("lon")),
    ])
    body = f"# {stop['position']}. {stop['city']}\n"
    if stop.get("notes"):
        body += f"\n{stop['notes']}\n"
    return fm + "\n" + body


def _landmark_note(lm, stops):
    stop = stops.get(lm.get("nearest_stop_id"))  # (note name, city) or None
    minutes = lm.get("drive_minutes")
    photos = [im for im in lm.get("images", []) if im["kind"] == "foto"]
    plans = [im for im in lm.get("images", []) if im["kind"] == "plano"]
    main_image = lm.get("url_image1") or (photos[0]["thumb"] if photos else None)
    fm = _frontmatter([
        ("tipo", "hito"),
        ("edificio", lm["name"]),
        ("arquitecto", lm["architect"]),
        ("ciudad", lm["city"]),
        ("direccion", lm.get("address")),
        ("año", lm.get("year")),
        ("estado", STATUS_LABEL.get(lm["status"], lm["status"])),
        ("parada_cercana", f"[[{stop[0]}]]" if stop else None),
        ("tiempo_coche_min", round(minutes) if minutes is not None else None),
        ("km", lm.get("drive_km")),
        ("tiempo_aproximado", lm.get("drive_source") == "estimado" or lm.get("geocode_status") == "ciudad"),
        ("archdaily", lm.get("url_archdaily")),
        ("arquitecturaviva", lm.get("url_av")),
        ("wikipedia", lm.get("wikipedia_url")),
        ("imagen", main_image),
        ("imagen2", lm.get("url_image2")),
        ("planos", len(plans) or None),
        ("lat", lm.get("lat")),
        ("lon", lm.get("lon")),
    ])
    body = f"# {lm['name']}\n\n**{lm['architect']}** — {lm['city']}"
    if lm.get("year"):
        body += f", {lm['year']}"
    body += "\n"
    if stop and minutes is not None:
        approx = "≈ " if lm.get("drive_source") == "estimado" else ""
        body += f"\n{approx}{round(minutes)} min en coche desde {stop[1]} ({lm.get('drive_km')} km)\n"
    if main_image:
        body += f"\n![]({main_image})\n"
    if lm.get("notes"):
        body += f"\n{lm['notes']}\n"
    if plans:
        body += "\n## Planos\n" + "".join(f"\n![{p['title']}]({p['thumb']})\n" for p in plans)
    if len(photos) > 1:
        body += "\n## Más fotos\n" + "".join(f"\n![{p['title']}]({p['thumb']})\n" for p in photos[1:4])
    return fm + "\n" + body


def _base_file(folder):
    # Syntax per https://obsidian.md/help/bases/syntax — bare property names in
    # filters, `note.x` / `file.name` in `order`.
    return f"""filters:
  and:
    - file.inFolder("{folder}")
    - tipo == "hito"
properties:
  edificio:
    displayName: Edificio
  arquitecto:
    displayName: Arquitecto
  ciudad:
    displayName: Ciudad
  estado:
    displayName: Estado
  parada_cercana:
    displayName: Desde
  tiempo_coche_min:
    displayName: Min en coche
  km:
    displayName: Km
  archdaily:
    displayName: ArchDaily
  arquitecturaviva:
    displayName: Arquitectura Viva
views:
  - type: table
    name: Todos
    order:
      - file.name
      - note.arquitecto
      - note.ciudad
      - note.estado
      - note.parada_cercana
      - note.tiempo_coche_min
      - note.archdaily
      - note.arquitecturaviva
    sort:
      - property: note.parada_cercana
        direction: ASC
      - property: note.tiempo_coche_min
        direction: ASC
  - type: table
    name: Curados
    filters:
      and:
        - estado == "Curado"
    groupBy:
      property: note.parada_cercana
      direction: ASC
    order:
      - file.name
      - note.arquitecto
      - note.ciudad
      - note.tiempo_coche_min
      - note.archdaily
      - note.arquitecturaviva
    sort:
      - property: note.tiempo_coche_min
        direction: ASC
  - type: table
    name: Posibles
    filters:
      and:
        - estado == "Posible"
    order:
      - file.name
      - note.arquitecto
      - note.ciudad
      - note.parada_cercana
      - note.tiempo_coche_min
    sort:
      - property: note.tiempo_coche_min
        direction: ASC
  - type: cards
    name: Fichas
    filters:
      and:
        - estado != "Descartado"
    image: note.imagen
    order:
      - file.name
      - note.arquitecto
      - note.ciudad
      - note.tiempo_coche_min
"""


# ------------------------------------------------------------- itinerary

WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

ITINERARY_CSS = """
  :root { --ink:#111; --muted:#666; --line:#d8d8d8; --soft:#f4f4f4; }
  * { box-sizing: border-box; }
  body { font-family: "Courier New", Courier, monospace; color: var(--ink); background: #fff;
         margin: 0 auto; max-width: 820px; padding: 32px 28px 60px; line-height: 1.45; }
  h1 { font-size: 22px; letter-spacing: 1px; text-transform: uppercase; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 13px; margin-bottom: 26px; }
  .day { border-top: 2px solid var(--ink); margin-top: 26px; padding-top: 10px; page-break-inside: avoid; }
  .day h2 { font-size: 15px; text-transform: uppercase; letter-spacing: 1px; margin: 0; }
  .day .where { color: var(--muted); font-size: 13px; margin: 2px 0 10px; }
  .day .daynote { background: var(--soft); padding: 6px 10px; font-size: 13px; margin-bottom: 10px; white-space: pre-wrap; }
  table { border-collapse: collapse; width: 100%; }
  td { vertical-align: top; padding: 5px 6px; border-bottom: 1px solid var(--line); }
  td.t { width: 58px; color: var(--muted); white-space: nowrap; }
  .arch { text-transform: uppercase; font-weight: bold; }
  .meta { color: var(--muted); font-size: 12px; }
  .note { font-style: italic; white-space: pre-wrap; }
  a { color: var(--ink); }
  .empty { color: var(--muted); font-style: italic; padding: 6px 0; }
  .foot { margin-top: 40px; border-top: 1px solid var(--line); padding-top: 8px;
          color: var(--muted); font-size: 11px; display: flex; gap: 14px; flex-wrap: wrap; }
  @media print { body { padding: 0 6mm; max-width: none; } .day { border-top-width: 1px; } a { text-decoration: none; } }
"""


def pretty_date(value):
    """'2026-03-14' -> 'sábado 14 de marzo de 2026' (unchanged if it isn't a date)."""
    try:
        d = datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return value or ""
    return f"{WEEKDAYS[d.weekday()]} {d.day} de {MONTHS[d.month - 1]} de {d.year}"


def _itinerary_item(item, landmarks, stops):
    time = f'<td class="t">{_esc(item.get("at_time") or "")}</td>'
    if item["kind"] == "nota":
        return f'<tr>{time}<td class="note">{_esc(item.get("text"))}</td></tr>'
    lm = landmarks.get(item.get("landmark_id"))
    if lm is None:
        return f'<tr>{time}<td class="muted">hito eliminado</td></tr>'
    bits = [lm["city"]]
    if lm.get("year"):
        bits.append(str(lm["year"]))
    if lm.get("address"):
        bits.append(lm["address"])
    minutes = lm.get("drive_minutes")
    stop = stops.get(lm.get("nearest_stop_id"))
    if minutes is not None and stop:
        approx = "≈" if lm.get("drive_source") == "estimado" else ""
        bits.append(f"{approx}{round(minutes)} min en coche desde {stop['city']}")
    links = []
    if lm.get("lat") is not None:
        links.append(f'<a href="https://www.google.com/maps/search/?api=1&query={lm["lat"]},{lm["lon"]}">mapa</a>')
    for label, key in (("ArchDaily", "url_archdaily"), ("Arquitectura Viva", "url_av"), ("Wikipedia", "wikipedia_url")):
        if lm.get(key):
            links.append(f'<a href="{_esc(lm[key])}">{label}</a>')
    meta = " · ".join(bits) + (" · " + " · ".join(links) if links else "")
    notes = f'<div class="meta">{_esc(lm["notes"])}</div>' if lm.get("notes") else ""
    return (f'<tr>{time}<td><span class="arch">{_esc(lm["architect"])}</span> — {_esc(lm["name"])}'
            f'<div class="meta">{meta}</div>{notes}</td></tr>')


def _esc(value):
    return (str(value if value is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def itinerary_html(payload):
    """-> (html, filename). A printable day-by-day itinerary (print to PDF from the browser)."""
    trip, days = payload["trip"], payload.get("days") or []
    landmarks = {lm["id"]: lm for lm in payload["landmarks"]}
    stops = {s["id"]: s for s in payload["stops"]}
    dated = [d["date"] for d in days if d.get("date")]
    span = ""
    if dated:
        span = pretty_date(min(dated)) + (f" — {pretty_date(max(dated))}" if min(dated) != max(dated) else "")
    parts = []
    for n, day in enumerate(days, start=1):
        head = f"Día {n}" + (f" · {pretty_date(day['date'])}" if day.get("date") else "")
        if day.get("title"):
            head += f" · {day['title']}"
        where = day.get("stop_city") or (stops.get(day.get("stop_id")) or {}).get("city")
        items = day.get("items") or []
        body = ("<table>" + "".join(_itinerary_item(it, landmarks, stops) for it in items) + "</table>"
                if items else '<div class="empty">sin nada planificado todavía</div>')
        parts.append(
            f'<section class="day"><h2>{_esc(head)}</h2>'
            + (f'<div class="where">Base: {_esc(where)}</div>' if where else "")
            + (f'<div class="daynote">{_esc(day["notes"])}</div>' if day.get("notes") else "")
            + body + "</section>")
    if not days:
        parts.append('<div class="empty">Este viaje todavía no tiene días. Créalos en la vista Itinerario.</div>')
    html = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Itinerario — {_esc(trip['name'])}</title>
<style>{ITINERARY_CSS}</style></head>
<body>
<h1>{_esc(trip['name'])}</h1>
<div class="sub">Itinerario{(' · ' + _esc(span)) if span else ''} · {len(days)} días</div>
{''.join(parts)}
<div class="foot"><span>archTrip</span><span>José Vargas-Zúñiga Soldevila, 2026</span>
<a href="https://ko-fi.com/josevzs">Ko-fi</a><a href="https://github.com/josevzs/archTrip">GitHub</a></div>
</body></html>
"""
    return html, f"itinerario-{slugify(trip['name'])}.html"


def _itinerary_note(payload):
    """The same itinerary as a Markdown note for the Obsidian export."""
    trip, days = payload["trip"], payload.get("days") or []
    landmarks = {lm["id"]: lm for lm in payload["landmarks"]}
    out = [f"# Itinerario — {trip['name']}", ""]
    for n, day in enumerate(days, start=1):
        head = f"## Día {n}" + (f" · {pretty_date(day['date'])}" if day.get("date") else "")
        if day.get("title"):
            head += f" · {day['title']}"
        out += [head, ""]
        if day.get("stop_city"):
            out += [f"*Base: {day['stop_city']}*", ""]
        if day.get("notes"):
            out += [day["notes"], ""]
        for it in day.get("items") or []:
            time = f"**{it['at_time']}** " if it.get("at_time") else ""
            if it["kind"] == "nota":
                out.append(f"- {time}*{it.get('text') or ''}*")
            else:
                lm = landmarks.get(it.get("landmark_id"))
                if lm:
                    note = safe_filename(f"{lm['name']} — {lm['architect']}")
                    out.append(f"- {time}[[{note}|{lm['name']}]] — {lm['architect']}, {lm['city']}")
        out.append("")
    if not days:
        out.append("*Sin días planificados.*")
    return "\n".join(out) + "\n"


def obsidian_zip(payload):
    """-> (zip_bytes, filename). Folder layout:
    <Viaje>/Viaje.base, <Viaje>/Ruta/NN Ciudad.md, <Viaje>/Hitos/Edificio — Arquitecto.md"""
    trip, stops, landmarks = payload["trip"], payload["stops"], payload["landmarks"]
    folder = safe_filename(trip["name"], "Viaje")
    stop_names = {}  # id -> (note name, city)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{folder}/Viaje.base", _base_file(folder))
        zf.writestr(f"{folder}/Itinerario.md", _itinerary_note(payload))
        for s in stops:
            note_name = safe_filename(f"{s['position']:02d} {s['city']}")
            stop_names[s["id"]] = (note_name, s["city"])
            zf.writestr(f"{folder}/Ruta/{note_name}.md", _stop_note(s))
        used = set()
        for lm in landmarks:
            note_name = safe_filename(f"{lm['name']} — {lm['architect']}")
            base, n = note_name, 2
            while note_name.lower() in used:
                note_name, n = f"{base} ({n})", n + 1
            used.add(note_name.lower())
            zf.writestr(f"{folder}/Hitos/{note_name}.md", _landmark_note(lm, stop_names))
    return buf.getvalue(), f"obsidian-{slugify(trip['name'])}.zip"
