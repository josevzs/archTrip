"""Paseos: hitos que en vez de un punto son un recorrido.

Un paseo es **un hito más**. Vive en la misma tabla, se cura igual (curado / tal vez /
descartado), se mete en un día del itinerario igual, puede llevar fotos y enlaces igual y sale en
los mismos PDF. Lo único propio es que guarda su geometría (`landmarks.geometry`, un GeoJSON de
tipo LineString o MultiLineString) y que **su punto es la entrada al recorrido**, no un centro:
así el tiempo en coche desde la parada, el mapa y el itinerario siguen teniendo sentido sin
inventarse nada.

De ahí que no haya una tabla aparte: separarlos obligaría a duplicar curación, itinerario, fotos,
diario y exportaciones para ganar únicamente una columna.

Se importan de un `.geojson` (`POST /api/trips/<id>/paseos`) o desde la propia plantilla de hitos,
poniendo `paseo` en la columna *Tipo* y, en *Recorrido*, la dirección de un `.geojson` o el
GeoJSON pegado. El formato que se espera es el de QGIS: una `FeatureCollection` donde cada
`Feature` lleva la geometría y, en `properties`, al menos `name`; y si están, se usan `city`,
`theme` (que hace de «arquitecto», es lo que describe el paseo), `status`, `notes`, `length_m` y
`access_lat`/`access_lon`."""
import json
import math

# lo que se reconoce como recorrido
LINE_TYPES = ("LineString", "MultiLineString")
# propiedades que se leen de cada Feature (las demás se guardan en las notas)
EXTRA_NOTES = ("theme", "source_quality", "geometry_status", "direction", "validation", "attribution")


def _parts(geometry):
    """Los tramos de un recorrido, siempre como lista de listas de puntos [lon, lat]."""
    if not geometry:
        return []
    if geometry.get("type") == "LineString":
        return [geometry.get("coordinates") or []]
    return [p for p in (geometry.get("coordinates") or []) if p]


def length_m(geometry):
    """Metros de recorrido, sumando todos los tramos (haversine, de sobra a esta escala)."""
    total = 0.0
    for part in _parts(geometry):
        for a, b in zip(part, part[1:]):
            r = math.pi / 180
            x = (math.sin((b[1] - a[1]) * r / 2) ** 2
                 + math.cos(a[1] * r) * math.cos(b[1] * r) * math.sin((b[0] - a[0]) * r / 2) ** 2)
            total += 12742000 * math.asin(math.sqrt(min(1.0, x)))
    return round(total, 1)


def entrance(geometry, props=None):
    """-> (lat, lon) de la entrada: la que diga el archivo (`access_lat`/`access_lon`) y, si no,
    el primer punto del primer tramo. El punto de un paseo es por dónde se empieza, no su centro:
    es lo que hace que el tiempo en coche y el mapa signifiquen algo."""
    props = props or {}
    lat, lon = props.get("access_lat"), props.get("access_lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return float(lat), float(lon)
    for part in _parts(geometry):
        if part:
            return float(part[0][1]), float(part[0][0])
    return None, None


def clean_geometry(geometry):
    """Deja la geometría en lo imprescindible (tipo y coordenadas) y comprueba que es un
    recorrido de verdad. Devuelve None si no lo es."""
    if not isinstance(geometry, dict) or geometry.get("type") not in LINE_TYPES:
        return None
    parts = _parts(geometry)
    limpio = []
    for part in parts:
        puntos = [[round(float(c[0]), 7), round(float(c[1]), 7)] for c in part
                  if isinstance(c, (list, tuple)) and len(c) >= 2]
        if len(puntos) >= 2:
            limpio.append(puntos)
    if not limpio:
        return None
    if geometry["type"] == "LineString":
        return {"type": "LineString", "coordinates": limpio[0]}
    return {"type": "MultiLineString", "coordinates": limpio}


def load(source, timeout=30):
    """El recorrido tal como puede venir en la plantilla: el GeoJSON pegado en la celda o la
    dirección de un `.geojson` publicado. -> el objeto ya cargado, o None si no se puede."""
    text = (source or "").strip()
    if not text:
        return None
    if text.startswith(("http://", "https://")):
        import requests
        resp = requests.get(text, headers={"User-Agent": "archTrip/0.1"}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    return json.loads(text)


def parse(data, default_name=None):
    """-> (paseos, errores). `data` es el GeoJSON ya cargado (FeatureCollection, Feature suelto o
    una geometría a secas). Cada paseo sale con la misma forma que una fila de la plantilla de
    hitos, para que el resto del sistema no note la diferencia.

    `default_name` es para el recorrido que viene pegado en una celda de la plantilla: ahí el
    nombre lo pone la fila, no el GeoJSON."""
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict) and data.get("type") in LINE_TYPES:
        data = {"type": "Feature", "geometry": data, "properties": {}}
    features = data.get("features") if isinstance(data, dict) and data.get("type") == "FeatureCollection" \
        else [data] if isinstance(data, dict) and data.get("type") == "Feature" else None
    if features is None:
        return [], ["El archivo no es un GeoJSON con recorridos (se esperaba FeatureCollection)"]

    out, errors = [], []
    for i, feature in enumerate(features, start=1):
        if not isinstance(feature, dict):
            errors.append(f"elemento {i}: no es un Feature")
            continue
        props = feature.get("properties") or {}
        geometry = clean_geometry(feature.get("geometry"))
        if geometry is None:
            tipo = (feature.get("geometry") or {}).get("type")
            errors.append(f"«{props.get('name') or ('elemento ' + str(i))}»: no es un recorrido"
                          + (f" (es {tipo})" if tipo else ""))
            continue
        name = (props.get("name") or props.get("walk_id") or default_name or "").strip()
        if not name:
            errors.append(f"elemento {i}: sin nombre (propiedad «name»)")
            continue
        lat, lon = entrance(geometry, props)
        notes = [str(props["notes"]).strip()] if props.get("notes") else []
        for key in EXTRA_NOTES[1:]:                     # el tema va como «arquitecto», no en notas
            if props.get(key):
                notes.append(f"{key}: {props[key]}")
        largo = props.get("length_m")
        out.append({
            "name": name,
            # en un paseo, lo que ocupa el sitio del arquitecto es de qué va
            "architect": (props.get("theme") or props.get("kind") or "Paseo").strip(),
            "city": (props.get("city") or "").strip(),
            "status": props.get("status") if props.get("status") in ("curado", "posible", "descartado") else None,
            "notes": "\n".join(notes) or None,
            "lat": lat, "lon": lon,
            "geometry": geometry,
            "length_m": float(largo) if isinstance(largo, (int, float)) else length_m(geometry),
            "ref": props.get("walk_id") or None,
        })
    return out, errors
