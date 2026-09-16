import io

from archtrip import excel
from conftest import make_xlsx


def test_route_template_roundtrip():
    stops, errors = excel.parse_route(io.BytesIO(excel.route_template()))
    assert errors == []
    assert stops == [{"position": 1, "city": "Oporto", "country": "Portugal", "notes": "Noche 1 y 2"}]


def test_landmarks_template_roundtrip():
    items, errors = excel.parse_landmarks(io.BytesIO(excel.landmarks_template()))
    assert errors == []
    assert len(items) == 1
    lm = items[0]
    assert (lm["name"], lm["architect"], lm["city"]) == ("Casa da Música", "Rem Koolhaas / OMA", "Oporto")
    assert lm["year"] == "2005" and lm["lat"] is None and lm["status"] is None
    assert lm["name_key"] == "casa da musica|rem koolhaas / oma"


def test_status_column():
    items, errors = excel.parse_landmarks(make_xlsx(["Edificio", "Arquitecto", "Ciudad", "Estado"], [
        ["A", "x", "y", "Posible"], ["B", "x", "y", "DESCARTADO"], ["C", "x", "y", "quizás"], ["D", "x", "y", None]]))
    assert [i["status"] for i in items] == ["posible", "descartado", None, None]
    assert len(errors) == 1 and "fila 4" in errors[0]


def test_headers_are_tolerant():
    buf = make_xlsx(["NOMBRE", "  arquitecto/a ", "Localidad", "año"], [["Casa X", "Y", "Z", 1990]])
    items, errors = excel.parse_landmarks(buf)
    assert errors == [] and items[0]["name"] == "Casa X" and items[0]["year"] == "1990"


def test_missing_required_column():
    items, errors = excel.parse_landmarks(make_xlsx(["Edificio", "Ciudad"], [["a", "b"]]))
    assert items == [] and "Arquitecto" in errors[0]


def test_row_errors_and_blank_rows():
    buf = make_xlsx(["Edificio", "Arquitecto", "Ciudad", "Latitud", "Longitud"], [
        ["Casa A", "Arq", "Oporto", None, None],
        [None, None, None, None, None],
        ["Casa B", None, "Oporto", None, None],
        ["Casa C", "Arq", "Oporto", 41.1, None],
        ["casa  a", "ARQ", "Oporto", None, None],
    ])
    items, errors = excel.parse_landmarks(buf)
    assert [i["name"] for i in items] == ["Casa A", "Casa C"]
    assert items[1]["lat"] is None
    assert any("fila 4" in e and "Arquitecto" in e for e in errors)
    assert any("fila 5" in e and "latitud" in e for e in errors)
    assert any("fila 6" in e and "repetido" in e for e in errors)


def test_route_without_numbers_keeps_sheet_order():
    stops, _ = excel.parse_route(make_xlsx(["Ciudad"], [["B"], ["A"], ["C"]]))
    assert [(s["position"], s["city"]) for s in stops] == [(1, "B"), (2, "A"), (3, "C")]
