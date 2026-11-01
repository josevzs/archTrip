"""El bot de Telegram del profesor (archtrip/telegram.py), sin red: lo que decide, con funciones
puras, y lo que hace, con un transporte de mentira que apunta cada llamada."""
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from archtrip import inbox, telegram
from archtrip.db import get_db
from test_api import fake_geo  # noqa: F401  (fixture)
from test_buzon import FakeInbox, jpeg, query, run, trip  # noqa: F401  (fixture)
from test_publicar import embedded

CHAT = "4242"


class FakeTG:
    """Telegram de mentira: apunta cada llamada y devuelve lo mínimo que usa el bot."""

    def __init__(self, updates=None):
        self.calls, self.updates, self.next_id = [], list(updates or []), 100

    def call(self, method, files=None, http_timeout=60, **params):
        self.calls.append((method, params, files))
        if method in ("sendPhoto", "sendMessage"):
            self.next_id += 1
            return {"message_id": self.next_id, "chat": {"id": int(CHAT)}}
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return out
        return True

    def of(self, method):
        return [(p, f) for m, p, f in self.calls if m == method]


@pytest.fixture(autouse=True)
def bot_env(monkeypatch):
    monkeypatch.setenv("ARCHTRIP_TELEGRAM_TOKEN", "123:secreto")
    monkeypatch.setenv("ARCHTRIP_TELEGRAM_CHAT", CHAT)
    telegram.sent.update(full=False, failing=None, summary_day=None)
    inbox._catalog_sent.clear()
    inbox.health.update(last_ok=None, failing_since=None, last_error=None, last_status=None)


@pytest.fixture
def box():
    return FakeInbox()


def with_db(app, fn, *args, **kw):
    with app.app_context():
        return fn(get_db(), *args, **kw)


def press(app, tg, data, message_id=101, photo=True, chat=CHAT):
    message = {"message_id": message_id, "chat": {"id": int(chat)}}
    if photo:
        message["photo"] = [{"file_id": "x"}]
    return with_db(app, telegram.on_callback, tg, {"id": "cb1", "data": data, "message": message})


def collected(app, box, sid="s1", landmark_id=None, **kw):
    box.add(sid, landmark_id, kw.pop("data", None) or jpeg(), **kw)
    run(app, box)
    return query(app, "SELECT * FROM inbox_photos WHERE id = ?", sid)[0]


# ------------------------------------------------------------ funciones puras

def test_the_caption_says_who_what_and_when():
    photo = {"author": "Lucía", "state": "pendiente", "taken_at": "2027-04-12T10:31:05", "landmark_name": "x"}
    text = telegram.caption(photo, {"name": "Museo Nezu", "architect": "Kengo Kuma", "city": "Tokio"}, 9)
    assert text == "📷 Lucía\nMuseo Nezu — Kengo Kuma\nTokio · día 9\nhecha el 2027-04-12 10:31"
    assert telegram.caption(dict(photo, state="publicada"), {"name": "N", "architect": "A"}).startswith("✓ Publicada\n")
    assert "Sin obra" in telegram.caption(dict(photo, taken_at=None), None)


def test_the_buttons_and_what_they_say():
    kb = telegram.review_keyboard("s1")
    flat = [b["callback_data"] for r in kb["inline_keyboard"] for b in r]
    assert flat == ["pub:s1", "rech:s1", "obra:s1"]
    assert "pub:s1" not in str(telegram.review_keyboard("s1", has_landmark=False))   # sin obra no se publica
    assert all(len(b) <= 64 for b in flat + ["set:" + "f" * 36 + ":123456"])          # el tope de Telegram
    assert telegram.parse_callback("pub:s1") == ("pub", "s1", None)
    assert telegram.parse_callback("set:s1:42") == ("set", "s1", 42)
    for bad in ("", "pub:", "borrar:s1", "set:s1:x", None, "pub:s1:extra"):
        assert telegram.parse_callback(bad) == (None, None, None)
    choices = [{"id": i, "name": "Una obra con un nombre larguísimo " * 2, "architect": "A"} for i in range(40)]
    kb = telegram.choose_keyboard("s1", choices, current=3)
    assert len(kb["inline_keyboard"]) == telegram.MAX_CHOICES + 1
    assert kb["inline_keyboard"][3][0]["text"].startswith("• ") and kb["inline_keyboard"][0][0]["text"].endswith("…")
    assert kb["inline_keyboard"][-1] == [{"text": "« volver", "callback_data": "back:s1"}]


def test_the_choices_are_the_landmarks_of_that_day():
    lms = {i: {"id": i, "name": n, "architect": "A"} for i, n in ((1, "Uno"), (2, "Dos"), (3, "Tres"), (4, "Cuatro"))}
    days = [{"date": "2027-04-12", "items": [{"kind": "hito", "landmark_id": 1}, {"kind": "nota"}, {"kind": "hito", "landmark_id": 2}]},
            {"date": "2027-04-13", "items": [{"kind": "hito", "landmark_id": 3}, {"kind": "hito", "landmark_id": 9}]}]
    ids = lambda out: [x["id"] for x in out]  # noqa: E731
    assert ids(telegram.day_choices(days, lms, "2027-04-12", 3)) == [1, 2]       # el día de la foto
    assert ids(telegram.day_choices(days, lms, "2027-05-01", 3)) == [3]          # si no, el de su obra
    assert ids(telegram.day_choices(days, lms, None, 4)) == [4, 2, 3, 1]          # si no, todas, por nombre


def test_the_daily_summary_text():
    assert telegram.summary_text(34, 11, 3) == "Hoy: 34 fotos de 11 alumnos, 3 sin aprobar."
    assert telegram.summary_text(1, 1, 0) == "Hoy: 1 foto de 1 alumno, todo aprobado."
    assert telegram.summary_text(0, 0, 2) == "Hoy: 0 fotos de 0 alumnos, 2 sin aprobar."
    assert telegram.summary_text(0, 0, 0) is None


def test_which_alarms_are_due():
    done = {"full": False, "failing": None, "summary_day": None}
    full = {"bytes": 5_400_000_000, "limite_bytes": 5 * 1024 ** 3}
    keys = lambda out: [k for k, _ in out]  # noqa: E731
    assert keys(telegram.alarms_due(1000, {}, full, done)) == ["full"]
    assert keys(telegram.alarms_due(1000, {}, full, dict(done, full=True))) == []              # una vez
    assert keys(telegram.alarms_due(1000, {}, {"bytes": 1, "limite_bytes": 9}, dict(done, full=True))) == ["full_ok"]
    failing = {"failing_since": 1000.0, "last_error": "El buzón no contesta"}
    assert keys(telegram.alarms_due(1000 + 3599, failing, None, done)) == []                    # menos de una hora
    out = telegram.alarms_due(1000 + 7200, failing, None, done)
    assert keys(out) == ["failing"] and "2 h" in out[0][1] and "no contesta" in out[0][1]
    assert keys(telegram.alarms_due(1000 + 9000, failing, None, dict(done, failing=1000.0))) == []   # mismo episodio
    assert keys(telegram.alarms_due(1000 + 9000, {"failing_since": None}, None, dict(done, failing=1000.0))) == ["failing_ok"]


def test_when_the_summary_is_due():
    tz = timezone(timedelta(hours=9))
    assert not telegram.summary_due(datetime(2027, 4, 12, 21, 59, tzinfo=tz), None)
    assert telegram.summary_due(datetime(2027, 4, 12, 22, 0, tzinfo=tz), None)
    assert not telegram.summary_due(datetime(2027, 4, 12, 23, 0, tzinfo=tz), "2027-04-12")
    assert telegram.summary_due(datetime(2027, 4, 13, 22, 5, tzinfo=tz), "2027-04-12")


# ------------------------------------------------------------ con base y transporte

def test_each_collected_photo_is_sent_once_with_its_buttons(app, client, box, trip):
    _, (a, b, _) = trip
    collected(app, box, "s1", a["id"], author="Lucía García")
    tg = FakeTG()
    assert with_db(app, telegram.send_reviews, tg) == 1
    params, files = tg.of("sendPhoto")[0]
    assert params["chat_id"] == CHAT and "Lucía García" in params["caption"] and a["name"] in params["caption"]
    assert "día 1" in params["caption"]
    assert [b_["callback_data"] for r in params["reply_markup"]["inline_keyboard"] for b_ in r] == ["pub:s1", "rech:s1", "obra:s1"]
    assert files["photo"][1][:2] == b"\xff\xd8"                                   # una foto de verdad, la copia
    assert query(app, "SELECT tg_message_id FROM inbox_photos")[0]["tg_message_id"] == 101
    assert with_db(app, telegram.send_reviews, tg) == 0                          # no se repite


def test_publish_approves_and_the_web_shows_it(app, client, box, trip):
    tid, (a, _, _) = trip
    collected(app, box, "s1", a["id"])
    tg = FakeTG()
    with_db(app, telegram.send_reviews, tg)
    press(app, tg, "pub:s1")
    assert query(app, "SELECT approved FROM landmark_images WHERE submission_id = 's1'")[0]["approved"] == 1
    rec = query(app, "SELECT state, decided_at FROM inbox_photos")[0]
    assert rec["state"] == "publicada" and rec["decided_at"]
    edit = tg.of("editMessageCaption")[0][0]
    assert edit["caption"].startswith("✓ Publicada") and edit["reply_markup"] == {"inline_keyboard": []}
    assert "Publicada" in tg.of("answerCallbackQuery")[-1][0]["text"]
    data = embedded(client.get(f"/api/trips/{tid}/export/html?alumnos=1").data.decode("utf-8"))
    assert any(im.get("author") == "Lucía" for lm in data["landmarks"] for im in lm["images"])
    press(app, tg, "rech:s1")                                                    # ya decidida: no se deshace así
    assert query(app, "SELECT state FROM inbox_photos")[0]["state"] == "publicada"


def test_reject_removes_the_image_but_keeps_the_original(app, client, box, trip):
    _, (a, _, _) = trip
    rec = collected(app, box, "s1", a["id"])
    im = query(app, "SELECT * FROM landmark_images WHERE submission_id = 's1'")[0]
    up = pathlib.Path(app.config["UPLOAD_DIR"])
    tg = FakeTG()
    press(app, tg, "rech:s1")
    assert query(app, "SELECT COUNT(*) AS n FROM landmark_images WHERE submission_id = 's1'")[0]["n"] == 0
    assert not (up / im["url"][len("/uploads/"):]).exists() and not (up / im["thumb"][len("/uploads/"):]).exists()
    assert pathlib.Path(rec["original"]).is_file()                               # el archivo, intacto
    after = query(app, "SELECT state, image_id FROM inbox_photos")[0]
    assert after == {"state": "rechazada", "image_id": None}


def test_change_landmark_lists_that_day_and_moves_the_photo(app, client, box, trip):
    _, (a, b, c) = trip
    collected(app, box, "s1", a["id"], taken_at="2027-04-12T10:00:00")
    tg = FakeTG()
    press(app, tg, "obra:s1")
    kb = tg.of("editMessageCaption")[-1][0]["reply_markup"]["inline_keyboard"]
    offered = [r[0]["callback_data"] for r in kb[:-1]]
    assert offered == [f"set:s1:{a['id']}", f"set:s1:{b['id']}"]                # las del día, sin la descartada
    press(app, tg, f"set:s1:{b['id']}")
    im = query(app, "SELECT landmark_id, approved FROM landmark_images WHERE submission_id = 's1'")[0]
    assert im == {"landmark_id": b["id"], "approved": 0}
    assert query(app, "SELECT landmark_id, landmark_name FROM inbox_photos")[0] == {"landmark_id": b["id"], "landmark_name": b["name"]}
    last = tg.of("editMessageCaption")[-1][0]
    assert b["name"] in last["caption"] and "pub:s1" in str(last["reply_markup"])
    press(app, tg, f"set:s1:{c['id']}")                                          # una descartada no se puede elegir
    assert query(app, "SELECT landmark_id FROM landmark_images WHERE submission_id = 's1'")[0]["landmark_id"] == b["id"]


def test_a_photo_without_landmark_gets_one_and_then_can_be_published(app, client, box, trip):
    _, (a, b, _) = trip
    client.delete(f"/api/landmarks/{b['id']}")
    collected(app, box, "s1", b["id"])
    tg = FakeTG()
    with_db(app, telegram.send_reviews, tg)
    params, files = tg.of("sendPhoto")[0]
    assert "Sin obra" in params["caption"] and "pub:s1" not in str(params["reply_markup"])
    assert files["photo"][1][:2] == b"\xff\xd8"                                   # hecha al vuelo del original
    press(app, tg, "pub:s1")
    assert query(app, "SELECT state FROM inbox_photos")[0]["state"] == "sin_obra"
    assert tg.of("answerCallbackQuery")[-1][0]["text"] == "Primero elige la obra."
    press(app, tg, f"set:s1:{a['id']}")
    assert query(app, "SELECT state FROM inbox_photos")[0]["state"] == "pendiente"
    im = query(app, "SELECT landmark_id, source, author FROM landmark_images WHERE submission_id = 's1'")[0]
    assert im == {"landmark_id": a["id"], "source": "alumno", "author": "Lucía"}
    press(app, tg, "pub:s1")
    assert query(app, "SELECT state FROM inbox_photos")[0]["state"] == "publicada"


def test_an_unreadable_photo_is_only_reported(app, client, box, trip):
    _, (a, _, _) = trip
    collected(app, box, "s1", a["id"], data=b"\xff\xd8\xff" + b"roto" * 30)
    tg = FakeTG()
    with_db(app, telegram.send_reviews, tg)
    assert not tg.of("sendPhoto")
    text = tg.of("sendMessage")[0][0]["text"]
    assert "no se puede abrir" in text and "fotos-alumnos" in text and "reply_markup" not in tg.of("sendMessage")[0][0]


def test_buttons_from_another_chat_are_ignored(app, client, box, trip):
    _, (a, _, _) = trip
    collected(app, box, "s1", a["id"])
    tg = FakeTG()
    assert press(app, tg, "pub:s1", chat="999") is None
    assert tg.calls == []
    assert query(app, "SELECT approved FROM landmark_images WHERE submission_id = 's1'")[0]["approved"] == 0


def test_housekeeping_alarms_and_the_summary(app, client, box, trip):
    _, (a, b, _) = trip
    collected(app, box, "s1", a["id"], author="Lucía")
    collected(app, box, "s2", b["id"], author="lucía ")                         # la misma, escrito distinto
    collected(app, box, "s3", a["id"], author="Bea")
    inbox.health.update(failing_since=1000.0, last_error="El buzón no contesta",
                        last_status={"bytes": 6 * 1024 ** 3, "limite_bytes": 5 * 1024 ** 3})
    tg = FakeTG()
    tz = timezone(timedelta(hours=9))
    local = datetime.now(tz).replace(hour=22, minute=30)
    with_db(app, telegram.housekeeping, tg, now=1000.0 + 4000, local_now=local)
    texts = [p["text"] for p, _ in tg.of("sendMessage")]
    assert any("buzón ha llegado" in t for t in texts) and any("1 h fallando" in t for t in texts)
    assert "Hoy: 3 fotos de 2 alumnos, 3 sin aprobar." in texts
    assert len(tg.of("sendPhoto")) == 3
    tg2 = FakeTG()
    with_db(app, telegram.housekeeping, tg2, now=1000.0 + 5000, local_now=local)
    assert tg2.calls == []                                                       # nada se repite


def test_poll_once_reads_buttons_and_moves_the_offset(app, client, box, trip):
    _, (a, _, _) = trip
    collected(app, box, "s1", a["id"])
    msg = {"message_id": 101, "chat": {"id": int(CHAT)}, "photo": []}
    tg = FakeTG(updates=[{"update_id": 7, "callback_query": {"id": "c", "data": "nada:raro", "message": msg}},
                         {"update_id": 8, "callback_query": {"id": "d", "data": "pub:s1", "message": msg}}])
    assert with_db(app, telegram.poll_once, tg, None) == 9
    params = tg.of("getUpdates")[0][0]
    assert params["allowed_updates"] == ["callback_query"] and "offset" not in params
    assert query(app, "SELECT state FROM inbox_photos")[0]["state"] == "publicada"
    with_db(app, telegram.poll_once, tg, 9)
    assert tg.of("getUpdates")[1][0]["offset"] == 9


def test_the_transport_hides_the_token_and_sends_files_as_multipart(monkeypatch):
    seen = []

    class Resp:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {"message_id": 1}}

    def post(url, **kw):
        seen.append((url, kw))
        return Resp()
    monkeypatch.setattr(telegram.requests, "post", post)
    tg = telegram.Transport("123:secreto")
    tg.call("sendMessage", chat_id="1", text="hola", reply_markup={"inline_keyboard": []})
    tg.call("sendPhoto", chat_id="1", reply_markup={"inline_keyboard": []}, files={"photo": ("a.jpg", b"x", "image/jpeg")})
    assert seen[0][0] == "https://api.telegram.org/bot123:secreto/sendMessage" and seen[0][1]["json"]["text"] == "hola"
    assert seen[1][1]["data"]["reply_markup"] == '{"inline_keyboard": []}' and "photo" in seen[1][1]["files"]

    def boom(url, **kw):
        raise telegram.requests.ConnectionError(f"no se pudo conectar a {url}")
    monkeypatch.setattr(telegram.requests, "post", boom)
    with pytest.raises(telegram.TelegramError) as err:
        tg.call("getMe")
    assert "secreto" not in str(err.value) and "***" in str(err.value)


def test_without_settings_the_bot_is_silent(monkeypatch):
    monkeypatch.delenv("ARCHTRIP_TELEGRAM_TOKEN")
    assert telegram.configured() is False
