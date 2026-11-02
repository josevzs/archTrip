// El buzón de fotos de los alumnos: un Worker de Cloudflare con un cubo R2 (FOTOS) y una base D1
// (BUZON). Es la mitad desechable del sistema: recibe, guarda lo justo y espera a que panda pase a
// recoger. Panda nunca recibe conexiones; es ella la que viene aquí, con BUZON_SECRET, a llevarse
// las fotos y borrarlas. Por eso el cubo no acumula, y por eso el tope de 5 GB no es un límite de
// gasto sino una alarma: si se alcanza, la recogida lleva días parada.
//
// Las funciones puras van arriba y se exportan para las pruebas (prueba.mjs). Las que también
// necesita el formulario (EXIF, distancia, ordenar) se le inyectan con .toString(), así que no
// pueden usar nada de fuera de su propio cuerpo.

export const MAX_BYTES = 25 * 1024 * 1024;           // una foto; un vídeo colado falla aquí
export const DAILY_PER_IP = 40;
export const BUCKET_LIMIT = 5 * 1024 * 1024 * 1024;  // 5 GB entre todo lo que espera a panda
export const MAX_AUTHOR = 60;
const TRIP_RE = /^[a-z0-9](?:[a-z0-9-]{0,56}[a-z0-9])?$/;   // el nombre del proyecto publicado

// ------------------------------------------------------------ funciones puras

// Qué imagen es, por sus primeros bytes: del Content-Type no hay que fiarse. HEIC/HEIF se
// reconoce para poder decirlo con claridad, pero no se admite: panda no sabría hacer las copias.
export function sniffImage(b) {
  if (!b || b.length < 12) return null;
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return { mime: 'image/jpeg', ext: 'jpg' };
  if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) return { mime: 'image/png', ext: 'png' };
  const ascii = (from, to) => String.fromCharCode(...b.slice(from, to));
  if (ascii(0, 4) === 'RIFF' && ascii(8, 12) === 'WEBP') return { mime: 'image/webp', ext: 'webp' };
  if (ascii(4, 8) === 'ftyp' && /^(heic|heix|hevc|heim|heis|hevm|mif1|msf1|avif)$/.test(ascii(8, 12))) return { heic: true };
  return null;
}

// Fecha y GPS del EXIF de un JPEG (lo que hacen los móviles). Lee solo lo necesario y devuelve
// {} si no hay nada o si algo no cuadra: un EXIF roto nunca puede impedir subir la foto.
export function readExif(bytes) {
  const out = {};
  try {
    const b = bytes;
    if (!b || b[0] !== 0xff || b[1] !== 0xd8) return out;
    let p = 2;
    while (p + 4 < b.length) {
      if (b[p] !== 0xff) return out;
      const marker = b[p + 1];
      const len = (b[p + 2] << 8) | b[p + 3];
      if (marker === 0xda || marker === 0xd9) return out;          // empiezan los datos: no hay EXIF
      if (marker === 0xe1 && String.fromCharCode(b[p + 4], b[p + 5], b[p + 6], b[p + 7]) === 'Exif') {
        const t = p + 10;                                           // cabecera TIFF
        const le = b[t] === 0x49;                                   // «II»: little endian
        const u16 = (o) => (le ? b[t + o] | (b[t + o + 1] << 8) : (b[t + o] << 8) | b[t + o + 1]);
        const u32 = (o) => (le ? (b[t + o] | (b[t + o + 1] << 8) | (b[t + o + 2] << 16) | (b[t + o + 3] << 24)) >>> 0
          : ((b[t + o] << 24) | (b[t + o + 1] << 16) | (b[t + o + 2] << 8) | b[t + o + 3]) >>> 0);
        const ifd = (o) => {
          const tags = {};
          const n = u16(o);
          for (let i = 0; i < n; i++) {
            const e = o + 2 + i * 12;
            tags[u16(e)] = { type: u16(e + 2), count: u32(e + 4), at: e + 8 };
          }
          return tags;
        };
        const ascii = (tag) => {
          const off = tag.count > 4 ? u32(tag.at) : tag.at;
          let s = '';
          for (let i = 0; i < tag.count - 1; i++) s += String.fromCharCode(b[t + off + i]);
          return s;
        };
        const rationals = (tag) => {
          const off = u32(tag.at);
          const v = [];
          for (let i = 0; i < tag.count; i++) { const den = u32(off + i * 8 + 4); v.push(den ? u32(off + i * 8) / den : 0); }
          return v;
        };
        const ifd0 = ifd(u32(4));
        if (ifd0[0x8769]) {                                          // Exif IFD: DateTimeOriginal
          const ex = ifd(u32(ifd0[0x8769].at));
          const d = ex[0x9003] || ex[0x9004];
          const m = d && /^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})/.exec(ascii(d));
          if (m) out.takenAt = `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}`;
        }
        if (ifd0[0x8825]) {                                          // GPS IFD
          const g = ifd(u32(ifd0[0x8825].at));
          if (g[2] && g[4]) {
            const deg = (v) => v[0] + v[1] / 60 + v[2] / 3600;
            const ref = (tag) => (tag ? String.fromCharCode(b[t + tag.at]) : '');
            let lat = deg(rationals(g[2])), lon = deg(rationals(g[4]));
            if (ref(g[1]) === 'S') lat = -lat;
            if (ref(g[3]) === 'W') lon = -lon;
            if (Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180 && (lat || lon)) {
              out.lat = Math.round(lat * 1e6) / 1e6;
              out.lon = Math.round(lon * 1e6) / 1e6;
            }
          }
        }
        return out;
      }
      p += 2 + len;
    }
  } catch (e) { /* EXIF a medias: se ignora */ }
  return out;
}

// Distancia en km entre dos puntos {lat, lon} (haversine).
export function distanceKm(a, b) {
  const r = Math.PI / 180;
  const x = Math.sin((b.lat - a.lat) * r / 2) ** 2
    + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin((b.lon - a.lon) * r / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(x));
}

// Las obras ordenadas por cercanía al sitio de la foto, con la distancia y la más cercana marcada.
// Ordena y marca, nada más: la obra la elige siempre el alumno. Sin punto, el orden de siempre.
export function sortByDistance(landmarks, point) {
  const list = landmarks.map((lm) => Object.assign({}, lm));
  if (!point || !Number.isFinite(point.lat) || !Number.isFinite(point.lon)) return list;
  list.forEach((lm) => {
    lm.km = Number.isFinite(lm.lat) && Number.isFinite(lm.lon) ? distanceKm(point, lm) : null;
  });
  list.sort((a, b) => (a.km == null) - (b.km == null) || (a.km || 0) - (b.km || 0));
  if (list.length && list[0].km != null) list[0].nearest = true;
  return list;
}

// El nombre tal como se guarda: sin espacios de más y con un tope.
export function cleanAuthor(text) {
  return String(text == null ? '' : text).replace(/\s+/g, ' ').trim().slice(0, MAX_AUTHOR);
}

// ¿Puede subir otra esta IP hoy? -> {ok, n} con la cuenta que quedaría.
export function quotaCheck(usedToday, limit = DAILY_PER_IP) {
  const n = Number(usedToday) || 0;
  return n >= limit ? { ok: false, n } : { ok: true, n: n + 1 };
}

export const MESSAGES = {
  author: 'Escribe tu nombre: las fotos van firmadas.',
  landmark: 'Elige la obra que sale en la foto.',
  notInCatalog: 'Esa obra no está en la lista de este viaje. Vuelve a cargar la página y elígela de nuevo.',
  noTrip: 'Este viaje no tiene buzón de fotos.',
  files: 'Sube una foto, y solo una: de una en una.',
  notImage: 'Eso no parece una foto (JPEG, PNG o WebP).',
  heic: 'Esta foto está en formato HEIC y no se puede recibir. En el iPhone: Ajustes → Cámara → Formatos → «Más compatible», o compártela como JPEG.',
  tooBig: 'La foto pasa de 25 MB. ¿No será un vídeo?',
  quota: 'Ya has subido 40 fotos hoy desde esta conexión. Mañana puedes seguir.',
  full: 'El buzón está lleno y no admite más fotos ahora mismo. El profesor ya está avisado; prueba mañana.',
};

// La validación de un envío, en el orden en que se le explica al alumno. Devuelve {ok, image}
// o {error, status}, con un mensaje que se puede enseñar tal cual.
export function validateSubmission(s) {
  if (!cleanAuthor(s.author)) return { error: MESSAGES.author, status: 400 };
  const id = Number(s.landmarkId);
  if (s.landmarkId == null || s.landmarkId === '' || !Number.isInteger(id) || id <= 0) return { error: MESSAGES.landmark, status: 400 };
  if (!s.catalog) return { error: MESSAGES.noTrip, status: 404 };
  if (!(s.catalog.landmarks || []).some((lm) => lm.id === id)) return { error: MESSAGES.notInCatalog, status: 400 };
  if (s.fileCount !== 1) return { error: MESSAGES.files, status: 400 };
  const image = sniffImage(s.head);
  if (image && image.heic) return { error: MESSAGES.heic, status: 415 };
  if (!image) return { error: MESSAGES.notImage, status: 415 };
  if (s.size > MAX_BYTES) return { error: MESSAGES.tooBig, status: 413 };
  if (!quotaCheck(s.usedToday).ok) return { error: MESSAGES.quota, status: 429 };
  if ((s.bucketBytes || 0) + s.size > BUCKET_LIMIT) return { error: MESSAGES.full, status: 507 };
  return { ok: true, image, landmarkId: id };
}

// Comparación en tiempo constante, para que el secreto no se adivine por lo que tarda.
export function sameSecret(given, expected) {
  if (typeof given !== 'string' || typeof expected !== 'string' || !expected) return false;
  let diff = given.length ^ expected.length;
  for (let i = 0; i < given.length; i++) diff |= given.charCodeAt(i) ^ expected.charCodeAt(i % expected.length);
  return diff === 0;
}

// sha256(ip + sal) en hexadecimal: nunca se guarda la IP. La sal es BUZON_SECRET.
export async function hashIp(ip, salt) {
  const data = new TextEncoder().encode(String(ip || '') + String(salt || ''));
  const digest = await crypto.subtle.digest('SHA-256', data);
  return [...new Uint8Array(digest)].map((x) => x.toString(16).padStart(2, '0')).join('');
}

// ------------------------------------------------------------ el Worker

const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
});
const today = () => new Date().toISOString().slice(0, 10);

async function catalogOf(env, trip) {
  const r = await env.BUZON.prepare('SELECT json FROM catalog WHERE trip = ?').bind(trip).first();
  if (!r) return null;
  try { return JSON.parse(r.json); } catch (e) { return null; }
}

async function bucketBytes(env) {
  const r = await env.BUZON.prepare("SELECT COALESCE(SUM(bytes), 0) AS b FROM submissions WHERE state = 'nuevo'").first();
  return (r && r.b) || 0;
}

async function submit(request, env, trip) {
  let form;
  try { form = await request.formData(); } catch (e) { return json({ error: MESSAGES.files }, 400); }
  const files = [];
  for (const [, v] of form.entries()) if (typeof v === 'object' && v && typeof v.arrayBuffer === 'function') files.push(v);
  const photo = files.length === 1 ? files[0] : null;
  const head = photo ? new Uint8Array(await photo.slice(0, 128 * 1024).arrayBuffer()) : null;
  const ipHash = await hashIp(request.headers.get('CF-Connecting-IP'), env.BUZON_SECRET);
  const day = today();
  const quota = await env.BUZON.prepare('SELECT n FROM quotas WHERE day = ? AND ip_hash = ?').bind(day, ipHash).first();
  const catalog = await catalogOf(env, trip);
  const check = validateSubmission({
    author: form.get('author'), landmarkId: form.get('landmark_id'), catalog, fileCount: files.length,
    head, size: photo ? photo.size : 0, usedToday: quota ? quota.n : 0, bucketBytes: await bucketBytes(env),
  });
  if (check.error) return json({ error: check.error }, check.status);

  const id = crypto.randomUUID();
  const key = `${trip}/${id}.${check.image.ext}`;
  const exif = readExif(head);
  const lm = catalog.landmarks.find((x) => x.id === check.landmarkId);
  await env.FOTOS.put(key, await photo.arrayBuffer(), { httpMetadata: { contentType: check.image.mime } });
  await env.BUZON.batch([
    env.BUZON.prepare(`INSERT INTO submissions (id, trip, landmark_id, landmark_name, author, object_key, bytes, mime,
                       taken_at, lat, lon, created_at, ip_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
      .bind(id, trip, lm.id, lm.name, cleanAuthor(form.get('author')), key, photo.size, check.image.mime,
        exif.takenAt || null, exif.lat ?? null, exif.lon ?? null, new Date().toISOString(), ipHash),
    env.BUZON.prepare(`INSERT INTO quotas (day, ip_hash, n) VALUES (?, ?, 1)
                       ON CONFLICT (day, ip_hash) DO UPDATE SET n = n + 1`).bind(day, ipHash),
    env.BUZON.prepare('DELETE FROM quotas WHERE day < ?').bind(day),
  ]);
  return json({ ok: true, id });
}

async function panda(request, env, path) {
  const auth = request.headers.get('Authorization') || '';
  if (!sameSecret(auth.replace(/^Bearer\s+/i, ''), env.BUZON_SECRET)) return json({ error: 'no autorizado' }, 401);
  const m = request.method;
  if (m === 'GET' && path === 'pendientes') {
    const r = await env.BUZON.prepare(`SELECT id, trip, landmark_id, landmark_name, author, object_key, bytes, mime,
                                       taken_at, lat, lon, created_at FROM submissions WHERE state = 'nuevo'
                                       ORDER BY created_at LIMIT 200`).all();
    return json({ items: r.results || [] });
  }
  if (m === 'GET' && path.startsWith('objeto/')) {
    const obj = await env.FOTOS.get(decodeURIComponent(path.slice('objeto/'.length)));
    if (!obj) return json({ error: 'no existe' }, 404);
    return new Response(obj.body, { headers: { 'content-type': (obj.httpMetadata && obj.httpMetadata.contentType) || 'application/octet-stream' } });
  }
  if (m === 'POST' && path === 'recogido') {
    let body;
    try { body = await request.json(); } catch (e) { return json({ error: 'JSON no válido' }, 400); }
    const ids = Array.isArray(body.ids) ? body.ids.filter((x) => typeof x === 'string').slice(0, 500) : [];
    if (!ids.length) return json({ done: 0 });
    const marks = ids.map(() => '?').join(',');
    const rows = (await env.BUZON.prepare(`SELECT id, object_key FROM submissions WHERE state = 'nuevo' AND id IN (${marks})`)
      .bind(...ids).all()).results || [];
    if (rows.length) {
      await env.FOTOS.delete(rows.map((r) => r.object_key));
      await env.BUZON.prepare(`UPDATE submissions SET state = 'recogido' WHERE id IN (${rows.map(() => '?').join(',')})`)
        .bind(...rows.map((r) => r.id)).run();
    }
    return json({ done: rows.length });
  }
  if (m === 'PUT' && path.startsWith('catalogo/')) {
    const trip = path.slice('catalogo/'.length);
    if (!TRIP_RE.test(trip) || trip === 'panda') return json({ error: 'viaje no válido' }, 400);
    const text = await request.text();
    if (text.length > 1024 * 1024) return json({ error: 'catálogo demasiado grande' }, 413);
    let data;
    try { data = JSON.parse(text); } catch (e) { return json({ error: 'JSON no válido' }, 400); }
    if (!data || !Array.isArray(data.landmarks)) return json({ error: 'falta la lista de obras' }, 400);
    await env.BUZON.prepare(`INSERT INTO catalog (trip, json, updated_at) VALUES (?, ?, ?)
                             ON CONFLICT (trip) DO UPDATE SET json = excluded.json, updated_at = excluded.updated_at`)
      .bind(trip, text, new Date().toISOString()).run();
    return json({ ok: true, landmarks: data.landmarks.length });
  }
  if (m === 'GET' && path === 'estado') {
    const day = today();
    const nuevos = await env.BUZON.prepare("SELECT COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b FROM submissions WHERE state = 'nuevo'").first();
    const hoy = await env.BUZON.prepare('SELECT COUNT(*) AS ips, COALESCE(SUM(n), 0) AS envios, COALESCE(MAX(n), 0) AS maximo FROM quotas WHERE day = ?').bind(day).first();
    return json({ nuevos: nuevos.n, bytes: nuevos.b, limite_bytes: BUCKET_LIMIT, dia: day,
      cuota: { ips: hoy.ips, envios: hoy.envios, maximo_por_ip: hoy.maximo, limite_por_ip: DAILY_PER_IP } });
  }
  return json({ error: 'no existe' }, 404);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const parts = url.pathname.replace(/^\/+|\/+$/g, '').split('/');
    if (parts[0] === 'panda') return panda(request, env, parts.slice(1).join('/'));
    const trip = parts[0];
    if (!trip || !TRIP_RE.test(trip)) return new Response('Buzón de fotos de archTrip.', { status: 404 });
    if (parts.length === 1 && request.method === 'GET') {
      if (!(await catalogOf(env, trip))) return new Response(MESSAGES.noTrip, { status: 404, headers: { 'content-type': 'text/plain; charset=utf-8' } });
      return new Response(formPage(trip), { headers: {
        'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store', 'x-robots-tag': 'noindex',
        'content-security-policy': "default-src 'self'; img-src 'self' blob: data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'",
      } });
    }
    if (parts[1] === 'obras' && request.method === 'GET') {
      const cat = await catalogOf(env, trip);
      return cat ? json(cat) : json({ error: MESSAGES.noTrip }, 404);
    }
    if (parts[1] === 'subir' && request.method === 'POST') return submit(request, env, trip);
    return new Response('No existe.', { status: 404 });
  },
};

// ------------------------------------------------------------ el formulario

export function formPage(trip) {
  return `<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<link rel="icon" href="data:,">
<title>Buzón de fotos</title>
<style>
  :root { --ink: #111; --muted: #666; --line: #d8d8d8; --bg: #fff; --soft: #f4f4f4; --ok: #2e7d4f; --warn: #b3261e; }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--ink); }
  body { font-family: "Courier New", Courier, monospace; font-size: 15px; line-height: 1.45; padding: 18px 16px 48px; max-width: 560px; margin: 0 auto; }
  h1 { font-size: 20px; letter-spacing: 1px; text-transform: uppercase; margin: 0 0 4px; }
  .muted { color: var(--muted); } .small { font-size: 13px; }
  label.f { display: block; margin: 18px 0 6px; font-size: 12px; letter-spacing: 2px; text-transform: uppercase; color: var(--muted); }
  input[type=text], input[type=search] { width: 100%; font: inherit; padding: 10px; border: 1px solid var(--ink); background: var(--bg); color: var(--ink); border-radius: 0; }
  .list { border: 1px solid var(--line); border-top: 0; max-height: 300px; overflow-y: auto; }
  .list button { display: block; width: 100%; text-align: left; font: inherit; padding: 9px 10px; border: 0; border-bottom: 1px solid var(--line); background: var(--bg); color: var(--ink); cursor: pointer; }
  .list button b { display: block; font-size: 12px; letter-spacing: 1px; text-transform: uppercase; }
  .list button .k { color: var(--muted); font-size: 12px; }
  .list button.on { background: var(--ink); color: var(--bg); }
  .list button.on .k { color: #ccc; }
  .list .near { font-size: 11px; background: var(--ok); color: #fff; padding: 0 5px; margin-left: 6px; }
  .chosen { margin-top: 6px; font-size: 13px; }
  .pick { display: block; border: 1px dashed var(--ink); padding: 18px; text-align: center; cursor: pointer; }
  .pick input { display: none; }
  .preview { display: none; margin-top: 10px; max-width: 100%; max-height: 320px; }
  .send { display: block; width: 100%; margin-top: 22px; font: inherit; font-size: 18px; font-weight: bold; padding: 14px; border: 0; background: var(--ink); color: var(--bg); cursor: pointer; }
  .send:disabled { opacity: .4; cursor: default; }
  .bar { height: 6px; border: 1px solid var(--line); margin-top: 10px; display: none; }
  .bar i { display: block; height: 100%; width: 0; background: var(--ink); }
  .msg { margin-top: 14px; padding: 10px 12px; border: 1px solid var(--line); background: var(--soft); display: none; }
  .msg.err { border-color: var(--warn); color: var(--warn); }
  .msg.ok { border-color: var(--ok); }
  .cabra { display: block; font-size: 44px; line-height: 1; }
  .again { font: inherit; margin-top: 10px; padding: 8px 12px; border: 1px solid var(--ink); background: var(--bg); color: var(--ink); cursor: pointer; }
</style></head>
<body>
<h1 id="trip">Buzón de fotos</h1>
<div class="muted small">Sube tus fotos de las obras del viaje, <b>de una en una</b>. Cada foto queda pendiente: solo se publica cuando el profesor la aprueba.</div>
<form id="f" novalidate>
  <label class="f" for="author">Autor</label>
  <input type="text" id="author" name="author" maxlength="${MAX_AUTHOR}" autocomplete="name" placeholder="Firma de la foto">

  <label class="f" for="photo-in">La foto</label>
  <label class="pick" id="pick"><span id="pick-t">Elegir una foto</span>
    <input type="file" id="photo-in" accept="image/jpeg,image/png,image/webp"></label>
  <img class="preview" id="preview" alt="">

  <label class="f" for="q">La obra que sale en la foto</label>
  <input type="search" id="q" placeholder="buscar por obra, arquitecto o ciudad" autocomplete="off">
  <div class="list" id="list" role="listbox"><div class="muted small" style="padding:10px">cargando la lista…</div></div>
  <div class="chosen" id="chosen"></div>

  <button class="send" id="send" type="submit" disabled>Enviar la foto</button>
  <div class="bar" id="bar"><i></i></div>
  <div class="msg" id="msg"></div>
</form>
<script>
(function () {
  'use strict';
  const TRIP = ${JSON.stringify(trip)};
  const MAX = ${MAX_BYTES};
  ${readExif.toString()}
  ${distanceKm.toString()}
  ${sortByDistance.toString()}
  const $ = (id) => document.getElementById(id);
  let catalog = [], shown = [], chosen = null, file = null, point = null;
  try { $('author').value = localStorage.getItem('buzon:autor') || ''; } catch (e) { /* sin almacenamiento */ }

  function say(text, kind) { const m = $('msg'); m.textContent = text; m.className = 'msg ' + (kind || ''); m.style.display = text ? 'block' : 'none'; }
  function ready() { $('send').disabled = !(file && chosen && $('author').value.trim()); }

  function draw() {
    const q = $('q').value.trim().toLowerCase();
    const items = shown.filter((lm) => !q || (lm.name + ' ' + lm.architect + ' ' + (lm.city || '')).toLowerCase().indexOf(q) >= 0);
    const list = $('list');
    list.textContent = '';
    if (!items.length) { const d = document.createElement('div'); d.className = 'muted small'; d.style.padding = '10px'; d.textContent = 'ninguna obra coincide'; list.appendChild(d); }
    items.forEach((lm) => {
      const b = document.createElement('button');
      b.type = 'button'; b.setAttribute('role', 'option');
      if (chosen && chosen.id === lm.id) b.className = 'on';
      const arch = document.createElement('b'); arch.textContent = lm.architect; b.appendChild(arch);
      b.appendChild(document.createTextNode(lm.name));
      const k = document.createElement('span'); k.className = 'k';
      k.textContent = ' · ' + [lm.city, lm.day ? 'día ' + lm.day : '', lm.km != null ? (lm.km < 1 ? Math.round(lm.km * 1000) + ' m' : lm.km.toFixed(1).replace('.', ',') + ' km') : ''].filter(Boolean).join(' · ');
      b.appendChild(k);
      if (lm.nearest) { const n = document.createElement('span'); n.className = 'near'; n.textContent = 'la más cercana a la foto'; b.appendChild(n); }
      b.addEventListener('click', () => { chosen = lm; $('chosen').textContent = '✓ ' + lm.architect + ' — ' + lm.name; draw(); ready(); });
      list.appendChild(b);
    });
  }

  fetch('/' + TRIP + '/obras').then((r) => r.json()).then((d) => {
    if (d.error) throw new Error(d.error);
    if (d.trip_name) { $('trip').textContent = 'Fotos · ' + d.trip_name; document.title = 'Buzón de fotos · ' + d.trip_name; }
    catalog = d.landmarks || []; shown = sortByDistance(catalog, point); draw();
  }).catch((e) => { $('list').textContent = e.message || 'No se ha podido cargar la lista de obras.'; });

  $('q').addEventListener('input', draw);
  $('author').addEventListener('input', ready);
  $('photo-in').addEventListener('change', async (e) => {
    say('');
    file = e.target.files && e.target.files[0] || null;
    point = null;
    if (file && file.size > MAX) { say('La foto pasa de 25 MB. ¿No será un vídeo?', 'err'); file = null; }
    else if (file && !/^image\\//.test(file.type || 'image/')) { say('Eso no parece una foto.', 'err'); file = null; }
    $('pick-t').textContent = file ? file.name : 'Elegir una foto';
    const img = $('preview');
    if (file) { img.src = URL.createObjectURL(file); img.style.display = 'block'; } else { img.style.display = 'none'; }
    if (file) {
      // el GPS de la foto ordena la lista y marca la obra más cercana; elegir, elige el alumno
      try {
        const ex = readExif(new Uint8Array(await file.slice(0, 128 * 1024).arrayBuffer()));
        if (ex.lat != null) point = { lat: ex.lat, lon: ex.lon };
      } catch (x) { /* sin EXIF */ }
    }
    shown = sortByDistance(catalog, point); draw(); ready();
  });

  $('f').addEventListener('submit', (e) => {
    e.preventDefault();
    if ($('send').disabled) return;
    const author = $('author').value.trim();
    try { localStorage.setItem('buzon:autor', author); } catch (x) { /* nada */ }
    const fd = new FormData();
    fd.append('author', author); fd.append('landmark_id', String(chosen.id)); fd.append('photo', file, file.name);
    const xhr = new XMLHttpRequest();
    $('send').disabled = true; $('bar').style.display = 'block'; say('');
    xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) $('bar').firstChild.style.width = Math.round(100 * ev.loaded / ev.total) + '%'; };
    xhr.onload = () => {
      $('bar').style.display = 'none'; $('bar').firstChild.style.width = '0';
      let d = {};
      try { d = JSON.parse(xhr.responseText); } catch (x) { /* respuesta rara */ }
      if (xhr.status === 200 && d.ok) {
        // sin sermón: la cabra dice que ha llegado (decisión del dueño, 2 nov 2026)
        say(' ', 'ok'); $('msg').innerHTML = '<span class="cabra">🐐</span>';
        const again = document.createElement('button'); again.type = 'button'; again.className = 'again'; again.textContent = 'Subir otra foto';
        again.addEventListener('click', () => { file = null; $('photo-in').value = ''; $('pick-t').textContent = 'Elegir una foto'; $('preview').style.display = 'none'; say(''); ready(); window.scrollTo(0, 0); });
        $('msg').appendChild(document.createElement('br')); $('msg').appendChild(again);
      } else {
        say(d.error || 'No se ha podido enviar (error ' + xhr.status + '). Prueba otra vez.', 'err');
        ready();
      }
    };
    xhr.onerror = () => { $('bar').style.display = 'none'; say('Sin conexión: la foto no se ha enviado. Prueba otra vez cuando tengas red.', 'err'); ready(); };
    xhr.open('POST', '/' + TRIP + '/subir');
    xhr.send(fd);
  });
})();
<\/script>
</body></html>`;
}
