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


def obsidian_zip(payload):
    """-> (zip_bytes, filename). Folder layout:
    <Viaje>/Viaje.base, <Viaje>/Ruta/NN Ciudad.md, <Viaje>/Hitos/Edificio — Arquitecto.md"""
    trip, stops, landmarks = payload["trip"], payload["stops"], payload["landmarks"]
    folder = safe_filename(trip["name"], "Viaje")
    stop_names = {}  # id -> (note name, city)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{folder}/Viaje.base", _base_file(folder))
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
