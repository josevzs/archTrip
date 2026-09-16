import pytest

from archtrip import geo

OPORTO = (41.1579, -8.6291)
LISBOA = (38.7223, -9.1393)
CASCAIS = (38.6979, -9.4215)


def test_haversine_oporto_lisboa():
    assert geo.haversine_km(*OPORTO, *LISBOA) == pytest.approx(274, abs=5)


def test_nearest_stop_skips_unlocated():
    stops = [
        {"id": 1, "city": "Oporto", "lat": OPORTO[0], "lon": OPORTO[1]},
        {"id": 2, "city": "Lisboa", "lat": LISBOA[0], "lon": LISBOA[1]},
        {"id": 3, "city": "Sin coords", "lat": None, "lon": None},
    ]
    stop, km = geo.nearest_stop(*CASCAIS, stops)
    assert stop["id"] == 2 and km == pytest.approx(25, abs=3)
    assert geo.nearest_stop(*CASCAIS, [stops[2]]) == (None, None)


def test_estimate_drive():
    minutes, km = geo.estimate_drive(100)
    assert km == 130.0
    assert minutes == pytest.approx(130 / 70 * 60, abs=0.1)


def test_nominatim_is_throttled(monkeypatch):
    calls = []

    class Resp:
        def raise_for_status(self): pass
        def json(self): return [{"lat": "41.1", "lon": "-8.6"}]

    monkeypatch.setattr(geo.requests, "get", lambda *a, **k: calls.append(k) or Resp())
    slept = []
    monkeypatch.setattr(geo.time, "sleep", lambda s: slept.append(s))
    geo._nominatim_last = 0.0
    assert geo.nominatim_geocode("Oporto") == (41.1, -8.6)
    assert geo.nominatim_geocode("Lisboa") == (41.1, -8.6)
    assert slept and slept[-1] > 0            # second call had to wait
    assert "User-Agent" in calls[0]["headers"]


def test_prefer_settlement_skips_province(monkeypatch):
    results = [
        {"lat": "36.11", "lon": "138.03", "addresstype": "province"},
        {"lat": "36.65", "lon": "138.19", "addresstype": "city"},
    ]

    class Resp:
        def raise_for_status(self): pass
        def json(self): return results

    seen = {}
    monkeypatch.setattr(geo.requests, "get", lambda *a, **k: seen.update(k) or Resp())
    monkeypatch.setattr(geo.time, "sleep", lambda s: None)
    assert geo.nominatim_geocode("Nagano, Japón", prefer_settlement=True) == (36.65, 138.19)
    assert seen["params"]["limit"] == 5
    assert geo.nominatim_geocode("Nagano, Japón") == (36.11, 138.03)   # landmarks: first hit
    assert seen["params"]["limit"] == 1


def test_simplified_name():
    from archtrip.enrich import simplified_name
    assert simplified_name("Tower of the Sun (Expo '70 Park)") == "Tower of the Sun"
    assert simplified_name("St. Mary's Cathedral, Tamatsukuri") == "St. Mary's Cathedral"
    assert simplified_name("Myōshin-ji (Taizō-in)") == "Myōshin-ji"
    assert simplified_name("Umeda Sky Building") is None
    assert simplified_name("(x)") is None
