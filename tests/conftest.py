import io

import pytest
from openpyxl import Workbook

from archtrip import create_app


@pytest.fixture
def app(tmp_path):
    app = create_app(db_path=tmp_path / "test.db")
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def make_xlsx(header, rows):
    """Build an in-memory .xlsx with one sheet."""
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


ROUTE_HEADER = ["Orden", "Ciudad", "País", "Notas"]
ROUTE_ROWS = [[1, "Oporto", "Portugal", None], [2, "Lisboa", "Portugal", "3 noches"]]

LANDMARK_HEADER = ["Edificio", "Arquitecto", "Ciudad", "Dirección", "Año", "Latitud", "Longitud",
                   "URL ArchDaily", "URL Arquitectura Viva", "URL Imagen 1", "URL Imagen 2", "Notas"]
LANDMARK_ROWS = [
    ["Casa da Música", "Rem Koolhaas", "Oporto", "Av. da Boavista 604", 2005, None, None, None, None, None, None, None],
    ["Museo de Serralves", "Álvaro Siza", "Oporto", None, 1999, 41.1596, -8.6597, "https://www.archdaily.com/x", None,
     "https://example.com/serralves.jpg", None, "Imprescindible"],
    ["Casa das Histórias Paula Rego", "Souto de Moura", "Cascais", None, 2009, None, None, None, None, None, None, None],
]


@pytest.fixture
def route_xlsx():
    return make_xlsx(ROUTE_HEADER, ROUTE_ROWS)


@pytest.fixture
def landmarks_xlsx():
    return make_xlsx(LANDMARK_HEADER, LANDMARK_ROWS)


def upload(client, url, buf, name="f.xlsx"):
    return client.post(url, data={"file": (buf, name)}, content_type="multipart/form-data")
