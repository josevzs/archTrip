-- La base D1 del buzón (binding BUZON). Solo guarda lo que espera a que panda lo recoja, la cuenta
-- de envíos de cada día y la lista de obras de cada viaje publicado. Se crea con:
--   npx wrangler d1 execute archtrip-buzon --remote --file=esquema.sql

CREATE TABLE IF NOT EXISTS submissions (
  id TEXT PRIMARY KEY,            -- uuid
  trip TEXT NOT NULL,             -- identificador del viaje publicado (el proyecto de Pages)
  landmark_id INTEGER NOT NULL,
  landmark_name TEXT NOT NULL,    -- copia, para que el aviso se lea sin consultar a panda
  author TEXT NOT NULL,           -- el nombre que escribe el alumno
  object_key TEXT NOT NULL,       -- la clave en R2
  bytes INTEGER NOT NULL,
  mime TEXT NOT NULL,
  taken_at TEXT,                  -- del EXIF, si venía
  lat REAL, lon REAL,             -- del EXIF, si venía
  created_at TEXT NOT NULL,
  ip_hash TEXT NOT NULL,          -- sha256(ip + sal), nunca la IP
  state TEXT NOT NULL DEFAULT 'nuevo'   -- nuevo | recogido
);
CREATE INDEX IF NOT EXISTS idx_submissions_state ON submissions (state, created_at);

CREATE TABLE IF NOT EXISTS quotas (day TEXT, ip_hash TEXT, n INTEGER, PRIMARY KEY (day, ip_hash));

CREATE TABLE IF NOT EXISTS catalog (trip TEXT PRIMARY KEY, json TEXT NOT NULL, updated_at TEXT NOT NULL);
