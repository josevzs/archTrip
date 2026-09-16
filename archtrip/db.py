import sqlite3
from pathlib import Path

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS route_stops (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id        INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    position       INTEGER NOT NULL,
    city           TEXT NOT NULL,
    country        TEXT,
    notes          TEXT,
    lat            REAL,
    lon            REAL,
    geocode_status TEXT NOT NULL DEFAULT 'pendiente'   -- pendiente | ok | fallido
);

CREATE TABLE IF NOT EXISTS landmarks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id         INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    architect       TEXT NOT NULL,
    city            TEXT NOT NULL,
    address         TEXT,
    year            TEXT,
    notes           TEXT,
    lat             REAL,
    lon             REAL,
    geocode_status  TEXT NOT NULL DEFAULT 'pendiente', -- pendiente | exacta | ciudad | fallido | manual
    url_archdaily   TEXT,
    url_av          TEXT,
    url_image1      TEXT,
    url_image2      TEXT,
    status          TEXT NOT NULL DEFAULT 'pendiente', -- pendiente | curado | posible | descartado
    nearest_stop_id INTEGER REFERENCES route_stops(id) ON DELETE SET NULL,
    drive_minutes   REAL,
    drive_km        REAL,
    drive_source    TEXT,                              -- osrm | estimado | NULL (pending)
    sort_order      INTEGER NOT NULL DEFAULT 0,
    name_key        TEXT NOT NULL,                     -- normalised name|architect, for upsert
    wikidata_id     TEXT,
    wikipedia_url   TEXT,
    images_status   TEXT NOT NULL DEFAULT 'pendiente', -- pendiente | ok | ninguna | fallido
    links_status    TEXT NOT NULL DEFAULT 'pendiente', -- pendiente | ok  (ArchDaily / AV lookup done)
    UNIQUE (trip_id, name_key)
);

CREATE TABLE IF NOT EXISTS landmark_images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    landmark_id INTEGER NOT NULL REFERENCES landmarks(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                         -- foto | plano
    url         TEXT NOT NULL,                         -- large image
    thumb       TEXT NOT NULL,                         -- ~500px wide
    title       TEXT,
    page_url    TEXT,                                  -- Commons file page (credit/licence)
    source      TEXT NOT NULL,                         -- wikidata | commons
    position    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_landmark_images ON landmark_images (landmark_id, kind, position);
"""

# Columns added after the first release; applied to existing databases on startup.
MIGRATIONS = [
    ("landmarks", "wikidata_id", "TEXT"),
    ("landmarks", "wikipedia_url", "TEXT"),
    ("landmarks", "images_status", "TEXT NOT NULL DEFAULT 'pendiente'"),
    ("landmarks", "links_status", "TEXT NOT NULL DEFAULT 'pendiente'"),
]


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()


def get_db():
    if "db" not in g:
        path = current_app.config["DB_PATH"]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = get_db()
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def row(cur):
    r = cur.fetchone()
    return dict(r) if r is not None else None
