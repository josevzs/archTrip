from archtrip import links


def test_matches_requires_the_name_or_most_of_it_plus_architect():
    assert links.matches("Casa da Musica / OMA", "Casa da Música", "Rem Koolhaas / OMA")
    assert links.matches("AD Classics: Casa da Música / OMA", "Casa da Música", "OMA")
    assert not links.matches("Miu Miu Aoyama Store / Herzog & de Meuron", "Prada Aoyama", "Herzog & de Meuron")
    assert not links.matches("H&dM: la sede de Prada en Tokio", "Prada Aoyama", "Herzog & de Meuron")
    assert links.matches("Sky House / Kiyonori Kikutake", "Sky House", "Kiyonori Kikutake")
    assert not links.matches("Sky Garden", "Sky House", "Kiyonori Kikutake")
    assert not links.matches("Museo de Arte Contemporáneo de Kanazawa", "21st Century Museum of Contemporary Art", "SANAA")
    assert links.matches("Museo del Siglo XXI, Kanazawa / SANAA", "Museo del siglo XXI", "SANAA")
    assert links.matches("Toyama Kirari / Kengo Kuma & Associates", "Toyama Kirari", "Kengo Kuma")
    assert links.matches("Kirari Glass Museum", "Toyama Kirari", "Kengo Kuma", extra="Kengo Kuma and Associates")
    assert not links.matches("Kirari Glass Museum", "Toyama Kirari", "Hiroshi Naito")          # half the name, wrong architect
    assert not links.matches("Casa da Musica Subway Station / Eduardo Souto de Moura", "Casa da Música", "Rem Koolhaas / OMA")
    assert links.matches("Casa da Musica Subway Station / Eduardo Souto de Moura", "Casa da Música", "Souto de Moura")
    # Spanish AV titles: the architect is implied by the tag, the city is allowed as an extra
    assert links.matches("Biblioteca Umimirai en Kanazawa", "Kanazawa Umimirai Library", "Coelacanth K&H", allow=["Kanazawa"], architect_known=True)
    assert links.matches("Casa da Música, Oporto (en construcción)", "Casa da Música", "OMA", allow=["Oporto"], architect_known=True)
    assert not links.matches("Casa en Burdeos", "Casa da Música", "OMA", architect_known=True)


def test_architect_words():
    assert links.architect_words("Rem Koolhaas / OMA") == {"rem", "koolhaas", "oma"}
    assert links.architect_words("Kengo Kuma & Associates") == {"kengo", "kuma"}
    assert links.architect_words("Tradicional · s. XVII") == set()
    assert links.architect_words("Yoshiro y Yoshio Taniguchi") == {"yoshiro", "yoshio", "taniguchi"}


def test_find_archdaily_prefers_confirmed_then_sole_candidate(monkeypatch):
    calls = []

    def search(kind, query):
        calls.append((kind, query))
        if kind == "projects":
            return [{"title": "Casa da Musica Subway Station / Souto de Moura", "url": "https://www.archdaily.com/1/station",
                     "offices": [{"name": "Souto de Moura"}]},
                    {"title": "Casa da musica / Alejandro Soffia", "url": "https://www.archdaily.com/2/chile", "offices": [{"name": "Alejandro Soffia"}]},
                    {"title": "Casa da Musica / OMA", "url": "https://www.archdaily.com/3/casa?ad_source=search", "offices": [{"name": "OMA"}]}]
        return []

    monkeypatch.setattr(links, "_ad_search", search)
    assert links.find_archdaily("Casa da Música", "Rem Koolhaas / OMA", "Oporto") == "https://www.archdaily.com/3/casa"
    assert calls[0] == ("projects", "Casa da Musica Rem Koolhaas OMA")   # plain-words query, name + architect
    # only the Chilean namesake left and no architect to confirm: its location gives it away
    monkeypatch.setattr(links, "_ad_search", lambda k, q: [{"title": "Casa da musica / Alejandro Soffia", "url": "https://www.archdaily.com/2/chile", "location": "La Florida, Chile"}] if k == "projects" else [])
    assert links.find_archdaily("Casa da Música", "Tradicional", "Oporto", "Portugal") is None
    # ...but a sole candidate in the right place (Spanish 'Oporto' vs the site's 'Porto') is accepted
    monkeypatch.setattr(links, "_ad_search", lambda k, q: [{"title": "Casa da Musica / Someone", "url": "https://www.archdaily.com/4/porto", "location": "Porto, Portugal"}] if k == "projects" else [])
    assert links.find_archdaily("Casa da Música", "Tradicional", "Oporto", "Portugal") == "https://www.archdaily.com/4/porto"
    assert links.location_ok("Tokyo, Japan", "Tokio", "Japón") and links.location_ok("Kanazawa, Ishikawa, Japan", "Kanazawa", "Japón")
    assert links.location_ok("La Florida, Chile", "Tokio", "Japón") is False and links.location_ok("", "Tokio") is None
    # two unconfirmed candidates -> unsure -> None
    monkeypatch.setattr(links, "_ad_search", lambda k, q: [{"title": "Casa da musica / A", "url": "https://x/1"}, {"title": "Casa da Musica / B", "url": "https://x/2"}] if k == "projects" else [])
    assert links.find_archdaily("Casa da Música", "Tradicional") is None


def test_find_av_by_architect_tag_then_free_text(monkeypatch):
    monkeypatch.setattr(links, "_av_tag", lambda a: "50045")
    monkeypatch.setattr(links, "_av_works", lambda tag: [
        {"title": "Casa en Burdeos", "url": "/obras/casa-en-burdeos", "kind": "obras"},
        {"title": "Casa da Música, Oporto (en construcción)", "url": "/obras/casa-da-musica-oporto", "kind": "obras"}])
    assert links.find_av("Casa da Música", "Rem Koolhaas / OMA", "Oporto") == "https://arquitecturaviva.com/obras/casa-da-musica-oporto"
    # no tag (traditional): free text must be unmistakable
    monkeypatch.setattr(links, "_av_tag", lambda a: None)
    monkeypatch.setattr(links, "_av_post", lambda page, form: ([{"title": "Templo Kiyomizu-dera", "url": "/obras/kiyomizu", "kind": "obras"},
                                                                {"title": "Kioto y sus templos", "url": "/articulos/kioto", "kind": "articulos"}], 2))
    assert links.find_av("Kiyomizu-dera", "Tradicional", "Kioto") == "https://arquitecturaviva.com/obras/kiyomizu"
    monkeypatch.setattr(links, "_av_post", lambda page, form: ([{"title": "Prada & Axiom Space", "url": "/articulos/x", "kind": "articulos"}], 1))
    assert links.find_av("Prada Aoyama", "Herzog & de Meuron") is None


def test_query_strips_punctuation_and_tolerant_swallows_site_errors():
    assert links._query("St. Mary's Cathedral, Tamatsukuri · Kenji Imai") == "St Mary s Cathedral Tamatsukuri Kenji Imai"
    import requests

    class R:
        def __init__(self, code): self.status_code = code
    def boom(code):
        def f(*a):
            raise requests.HTTPError(response=R(code))
        return f
    assert links.tolerant(boom(500), "x") is None
    assert links.tolerant(boom(404), "x") is None
    try:
        links.tolerant(boom(429), "x"); assert False
    except requests.HTTPError:
        pass
    assert links.tolerant(lambda n: "ok", "x") == "ok"
