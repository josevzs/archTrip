"""Viaje de pruebas para los flujos de navegador (cdp.mjs, itinerario.mjs, admin.mjs).

    python tests/browser/seed.py [base-url]      # por defecto http://127.0.0.1:8000

Borra los viajes que haya en esa instancia y deja uno con: ruta de dos paradas, tres hitos
conocidos (con fotos reales de Commons), un cuarto en las mismas coordenadas que otro —para
la agrupación del mapa— y un quinto que no existe, que se queda 'ciudad' y alimenta el filtro
"Sin localizar". Los flujos de navegador son destructivos (borran un hito, cambian estados),
así que conviene volver a sembrar antes de cada pasada. Imprime el id del viaje.
"""
import io
import sys

import requests
from openpyxl import Workbook

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
s = requests.Session()

for t in s.get(f"{BASE}/api/trips", timeout=30).json():
    s.delete(f"{BASE}/api/trips/{t['id']}", timeout=30)
tid = s.post(f"{BASE}/api/trips", json={"name": "Portugal 2027"}, timeout=30).json()["id"]


def sheet(header, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


s.post(f"{BASE}/api/trips/{tid}/route",
       files={"file": ("ruta.xlsx", sheet(["Orden", "Ciudad", "País", "Notas"],
                                          [[1, "Oporto", "Portugal", None], [2, "Lisboa", "Portugal", None]]))},
       timeout=60)

landmarks = [
    ["Casa da Música", "Rem Koolhaas", "Oporto", 41.1587, -8.6307, 2005],
    ["Museo de Serralves", "Álvaro Siza", "Oporto", 41.1596, -8.6597, 1999],
    ["Casa das Histórias", "Souto de Moura", "Cascais", 38.6975, -9.4215, 2009],
    ["Vecino de la Casa da Música", "Otro", "Oporto", 41.1587, -8.6307, None],   # mismo punto: agrupación
    ["Edificio Fantasma de Prueba", "Anónimo", "Oporto", None, None, 1900],      # no existe: se queda en 'ciudad'
]
print(s.post(f"{BASE}/api/trips/{tid}/landmarks",
             files={"file": ("hitos.xlsx", sheet(["Edificio", "Arquitecto", "Ciudad", "Latitud", "Longitud", "Año"],
                                                 landmarks))}, timeout=120).json())

for _ in range(300):                       # fotos y enlaces reales: tarda ~1 min
    if s.post(f"{BASE}/api/trips/{tid}/enrich/next", timeout=180).json()["done"]:
        break
data = s.get(f"{BASE}/api/trips/{tid}", timeout=60).json()

# algo de curado y un borrado, para que el diario de sesiones tenga material (admin.mjs)
lms = data["landmarks"]
s.patch(f"{BASE}/api/landmarks/{lms[0]['id']}", json={"status": "curado"}, timeout=30)
s.patch(f"{BASE}/api/landmarks/{lms[1]['id']}", json={"status": "descartado", "notes": "queda lejos"}, timeout=30)
img = s.post(f"{BASE}/api/landmarks/{lms[2]['id']}/images",
             json={"url": "https://example.com/prueba.jpg", "kind": "foto"}, timeout=30).json()
s.delete(f"{BASE}/api/landmarks/{lms[2]['id']}/images/{img['images'][0]['id']}", timeout=30)

data = s.get(f"{BASE}/api/trips/{tid}", timeout=60).json()
print("viaje", tid, [(lm["name"][:28], lm["geocode_status"], lm["status"], len(lm["images"])) for lm in data["landmarks"]])
