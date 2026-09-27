"""Exports: a standalone single-file HTML copy, a printable itinerary in PDF, and an Obsidian
Bases vault folder as ZIP."""
import base64
import io
import json
import os
import re
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

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

# Un PDF para imprimir y repartir: sin barra de créditos, solo el pie de "generado con".
PDF_FONTS = [                                   # primer archivo que exista, en este orden
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",      # Docker (fonts-dejavu-core)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/cour.ttf",
]
PAGE_MARGIN = 18 * mm


def pretty_date(value):
    """'2026-03-14' -> 'sábado 14 de marzo de 2026' (unchanged if it isn't a date)."""
    try:
        d = datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return value or ""
    return f"{WEEKDAYS[d.weekday()]} {d.day} de {MONTHS[d.month - 1]} de {d.year}"


def today_long():
    d = datetime.now()
    return f"{d.day} de {MONTHS[d.month - 1]} de {d.year}"


def _esc(value):
    return (str(value if value is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _mono_font():
    """Courier del sistema con tildes y macrones; si no hay ninguno, el Courier básico del PDF
    (que solo llega a Latin-1, así que ahí se quitan los acentos raros)."""
    for path in [os.environ.get("ARCHTRIP_PDF_FONT")] + PDF_FONTS:
        if path and Path(path).exists():
            try:
                pdfmetrics.registerFont(TTFont("archtrip-mono", path))
                return "archtrip-mono", True
            except Exception:
                continue
    try:                       # el que trae reportlab: vale, pero sin macrones
        pdfmetrics.registerFont(TTFont("archtrip-mono", str(Path(reportlab.__file__).parent / "fonts" / "Vera.ttf")))
        return "archtrip-mono", False
    except Exception:
        return "Courier", False


def _latin1(text):
    """Quita lo que no entra en Latin-1 conservando la letra: Hōryū -> Horyu."""
    out = unicodedata.normalize("NFKD", text)
    out = "".join(c for c in out if not unicodedata.combining(c))
    return out.encode("latin-1", "replace").decode("latin-1")


def itinerary_rows(payload):
    """El itinerario como texto, listo para pintar (y fácil de comprobar en los tests):
    [{'head':…, 'base':…, 'notes':…, 'items':[(hora, línea, detalle)]}]"""
    days = payload.get("days") or []
    landmarks = {lm["id"]: lm for lm in payload["landmarks"]}
    stops = {s["id"]: s for s in payload["stops"]}
    out = []
    for n, day in enumerate(days, start=1):
        head = f"Día {n}" + (f" · {pretty_date(day['date'])}" if day.get("date") else "")
        if day.get("title"):
            head += f" · {day['title']}"
        rows = []
        for it in day.get("items") or []:
            time = it.get("at_time") or ""
            if it["kind"] == "nota":
                rows.append((time, it.get("text") or "", ""))
                continue
            lm = landmarks.get(it.get("landmark_id"))
            if lm is None:
                rows.append((time, "hito eliminado", ""))
                continue
            bits = [lm["city"]]
            if lm.get("year"):
                bits.append(str(lm["year"]))
            if lm.get("address"):
                bits.append(lm["address"])
            minutes, stop = lm.get("drive_minutes"), stops.get(lm.get("nearest_stop_id"))
            if minutes is not None and stop:
                approx = "≈" if lm.get("drive_source") == "estimado" else ""
                bits.append(f"{approx}{round(minutes)} min en coche desde {stop['city']}")
            if lm.get("notes"):
                bits.append(lm["notes"])
            rows.append((time, f"{lm['architect'].upper()} — {lm['name']}", " · ".join(bits)))
        out.append({"head": head,
                    "base": day.get("stop_city") or (stops.get(day.get("stop_id")) or {}).get("city") or "",
                    "notes": day.get("notes") or "", "items": rows})
    return out


class _Numbered(canvas.Canvas):
    """Pie con "generado con" y la paginación, que necesita saber el total: dos pasadas."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._pages = []

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self._foot(total)
            super().showPage()
        super().save()

    def _foot(self, total):
        self.setFont(self._archtrip_font, 7.5)
        self.setFillColorRGB(.45, .45, .45)
        self.drawString(PAGE_MARGIN, 12 * mm, self._archtrip_note)
        self.drawRightString(A4[0] - PAGE_MARGIN, 12 * mm, f"página {self._pageNumber} de {total}")


def itinerary_pdf(payload):
    """-> (pdf_bytes, filename). Día a día, para imprimir o mandar por correo."""
    trip = payload["trip"]
    font, unicode_ok = _mono_font()
    clean = (lambda t: t) if unicode_ok else _latin1
    styles = {
        "title": ParagraphStyle("t", fontName=font, fontSize=15, leading=19, spaceAfter=2),
        "sub": ParagraphStyle("s", fontName=font, fontSize=8.5, leading=12, textColor=colors.HexColor("#666666")),
        "day": ParagraphStyle("d", fontName=font, fontSize=10.5, leading=14, spaceBefore=2, spaceAfter=1),
        "base": ParagraphStyle("b", fontName=font, fontSize=8.5, leading=11, textColor=colors.HexColor("#666666")),
        "note": ParagraphStyle("n", fontName=font, fontSize=8.5, leading=11.5, textColor=colors.HexColor("#333333"),
                               backColor=colors.HexColor("#f4f4f4"), borderPadding=4, spaceBefore=3, spaceAfter=3),
        "time": ParagraphStyle("h", fontName=font, fontSize=9, leading=12, textColor=colors.HexColor("#666666")),
        "item": ParagraphStyle("i", fontName=font, fontSize=9, leading=12),
        "meta": ParagraphStyle("m", fontName=font, fontSize=7.5, leading=10, textColor=colors.HexColor("#666666")),
        "empty": ParagraphStyle("e", fontName=font, fontSize=8.5, leading=11, textColor=colors.HexColor("#999999")),
    }
    rows = itinerary_rows(payload)
    dated = [d["date"] for d in (payload.get("days") or []) if d.get("date")]
    span = ""
    if dated:
        span = pretty_date(min(dated)) + (f" — {pretty_date(max(dated))}" if min(dated) != max(dated) else "")

    story = [Paragraph(_esc(clean(trip["name"])).upper(), styles["title"]),
             Paragraph(_esc(clean("Itinerario" + (f" · {span}" if span else "") + f" · {len(rows)} días")), styles["sub"]),
             Spacer(1, 6 * mm)]
    if not rows:
        story.append(Paragraph(_esc(clean("Este viaje todavía no tiene días: créalos en la vista Itinerario.")), styles["empty"]))
    for day in rows:
        block = [HRFlowable(width="100%", thickness=1, color=colors.HexColor("#111111"), spaceAfter=4),
                 Paragraph(_esc(clean(day["head"])).upper(), styles["day"])]
        if day["base"]:
            block.append(Paragraph(_esc(clean("Base: " + day["base"])), styles["base"]))
        if day["notes"]:
            block.append(Paragraph(_esc(clean(day["notes"])), styles["note"]))
        if day["items"]:
            data = []
            for time, line, meta in day["items"]:
                cell = [Paragraph(_esc(clean(line)), styles["item"])]
                if meta:
                    cell.append(Paragraph(_esc(clean(meta)), styles["meta"]))
                data.append([Paragraph(_esc(clean(time)), styles["time"]), cell])
            table = Table(data, colWidths=[16 * mm, None], hAlign="LEFT")
            table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LINEBELOW", (0, 0), (-1, -2), .4, colors.HexColor("#d8d8d8")),
            ]))
            block.append(table)
        else:
            block.append(Paragraph(_esc(clean("sin nada planificado todavía")), styles["empty"]))
        block.append(Spacer(1, 5 * mm))
        # el día entero junto si cabe; si no, que parta por donde pueda
        story.append(KeepTogether(block) if len(day["items"]) <= 8 else block[0])
        if len(day["items"]) > 8:
            story.extend(block[1:])

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=f"Itinerario — {clean(trip['name'])}", author="", creator="archTrip",
                            subject="", leftMargin=PAGE_MARGIN, rightMargin=PAGE_MARGIN,
                            topMargin=PAGE_MARGIN, bottomMargin=20 * mm)
    _Numbered._archtrip_font = font
    _Numbered._archtrip_note = clean(f"Generado con el sistema archTrip el {today_long()}")
    doc.build(story, canvasmaker=_Numbered)
    return buf.getvalue(), f"itinerario-{slugify(trip['name'])}.pdf"


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
