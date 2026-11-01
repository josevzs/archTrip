// Pruebas del buzón, sin red:  node --test buzon/prueba.mjs   (o `npm test` dentro de buzon/)
// Las funciones puras, siempre. El Worker entero, contra un D1 de mentira hecho con SQLite de
// verdad (node:sqlite, Node 22) y el mismo esquema.sql, y un R2 en memoria; si node:sqlite no
// está, esas se saltan.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import worker, {
  sniffImage, readExif, distanceKm, sortByDistance, validateSubmission, quotaCheck, cleanAuthor,
  sameSecret, hashIp, formPage, MESSAGES, MAX_BYTES, BUCKET_LIMIT, DAILY_PER_IP,
} from './worker.js';

// ------------------------------------------------------------ un JPEG con EXIF, hecho a mano

// Fecha y GPS en un bloque TIFF, en el orden de bytes que se pida («II» o «MM»).
function jpegWithExif({ le = true, date = '2026:04:12 10:31:05', lat = null, lon = null } = {}) {
  const parts = [];
  const u16 = (v) => (le ? [v & 255, v >> 8] : [v >> 8, v & 255]);
  const u32 = (v) => (le ? [v & 255, (v >> 8) & 255, (v >> 16) & 255, (v >>> 24) & 255] : [(v >>> 24) & 255, (v >> 16) & 255, (v >> 8) & 255, v & 255]);
  const entry = (tag, type, count, value) => [...u16(tag), ...u16(type), ...u32(count), ...value];
  // disposición: cabecera (8) · IFD0 (2 + 2·12 + 4 = 30) · ExifIFD (2 + 12 + 4 = 18) · GPS IFD (2 + 4·12 + 4 = 54) · datos
  const ifd0 = 8, exifIfd = ifd0 + 30, gpsIfd = exifIfd + 18, data = gpsIfd + 54;
  const dateBytes = [...new TextEncoder().encode(date), 0];
  const dms = (v) => { const a = Math.abs(v), d = Math.floor(a), m = Math.floor((a - d) * 60), s = Math.round(((a - d) * 60 - m) * 60 * 1000); return [[d, 1], [m, 1], [s, 1000]]; };
  const rat = (list) => list.flatMap(([n, den]) => [...u32(n), ...u32(den)]);
  const latOff = data + dateBytes.length, lonOff = latOff + 24;
  parts.push(...(le ? [0x49, 0x49] : [0x4d, 0x4d]), ...u16(42), ...u32(ifd0));
  parts.push(...u16(2), ...entry(0x8769, 4, 1, u32(exifIfd)), ...entry(0x8825, 4, 1, u32(lat == null ? 0 : gpsIfd)), ...u32(0));
  parts.push(...u16(1), ...entry(0x9003, 2, dateBytes.length, u32(data)), ...u32(0));
  const ref = (c) => [c.charCodeAt(0), 0, 0, 0];
  parts.push(...u16(4),
    ...entry(1, 2, 2, ref(lat != null && lat < 0 ? 'S' : 'N')), ...entry(2, 5, 3, u32(latOff)),
    ...entry(3, 2, 2, ref(lon != null && lon < 0 ? 'W' : 'E')), ...entry(4, 5, 3, u32(lonOff)), ...u32(0));
  parts.push(...dateBytes, ...(lat == null ? new Array(48).fill(0) : [...rat(dms(lat)), ...rat(dms(lon))]));
  const tiff = parts;
  const app1 = [...new TextEncoder().encode('Exif'), 0, 0, ...tiff];
  const len = app1.length + 2;
  return new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0, 16, 0x4a, 0x46, 0x49, 0x46, 0, 1, 1, 0, 0, 1, 0, 1, 0, 0,
    0xff, 0xe1, len >> 8, len & 255, ...app1, 0xff, 0xda, 0, 2, 0xff, 0xd9]);
}
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48]);
const ascii = (s) => [...s].map((c) => c.charCodeAt(0));
const WEBP = new Uint8Array([...ascii('RIFF'), 0, 0, 0, 0, ...ascii('WEBPVP8 ')]);
const HEIC = new Uint8Array([0, 0, 0, 24, ...ascii('ftypheic'), 0, 0, 0, 0]);

// ------------------------------------------------------------ funciones puras

test('reconoce la imagen por sus primeros bytes, no por lo que diga el navegador', () => {
  assert.deepEqual(sniffImage(jpegWithExif()), { mime: 'image/jpeg', ext: 'jpg' });
  assert.deepEqual(sniffImage(PNG), { mime: 'image/png', ext: 'png' });
  assert.deepEqual(sniffImage(WEBP), { mime: 'image/webp', ext: 'webp' });
  assert.deepEqual(sniffImage(HEIC), { heic: true });
  assert.equal(sniffImage(new Uint8Array([0, 0, 0, 24, ...ascii('ftypmp42'), 0, 0])), null);   // un vídeo
  assert.equal(sniffImage(new TextEncoder().encode('<html>hola</html>')), null);
  assert.equal(sniffImage(new Uint8Array([0xff, 0xd8])), null);
});

test('lee la fecha y el GPS del EXIF, en los dos órdenes de bytes', () => {
  for (const le of [true, false]) {
    const ex = readExif(jpegWithExif({ le, lat: 35.0394, lon: 135.7292 }));
    assert.equal(ex.takenAt, '2026-04-12T10:31:05');
    assert.ok(Math.abs(ex.lat - 35.0394) < 1e-4 && Math.abs(ex.lon - 135.7292) < 1e-4, JSON.stringify(ex));
  }
  const sur = readExif(jpegWithExif({ lat: -33.8568, lon: -70.6483 }));
  assert.ok(sur.lat < 0 && sur.lon < 0);
});

test('un EXIF sin GPS, roto o que no es JPEG da {} sin romper nada', () => {
  assert.deepEqual(Object.keys(readExif(jpegWithExif())), ['takenAt']);
  assert.deepEqual(readExif(PNG), {});
  assert.deepEqual(readExif(jpegWithExif({ lat: 1, lon: 2 }).slice(0, 60)), {});
  assert.deepEqual(readExif(null), {});
  assert.deepEqual(readExif(new Uint8Array(0)), {});
});

test('distancia entre dos puntos', () => {
  const madrid = { lat: 40.4168, lon: -3.7038 }, barcelona = { lat: 41.3874, lon: 2.1686 };
  assert.ok(Math.abs(distanceKm(madrid, barcelona) - 505) < 5);
  assert.equal(distanceKm(madrid, madrid), 0);
});

test('ordenar por cercanía marca la más próxima y no elige nada', () => {
  const obras = [
    { id: 1, name: 'Lejos', lat: 35.6, lon: 139.7 },
    { id: 2, name: 'Sin sitio' },
    { id: 3, name: 'Cerca', lat: 35.04, lon: 135.73 },
  ];
  const out = sortByDistance(obras, { lat: 35.0394, lon: 135.7292 });
  assert.deepEqual(out.map((x) => x.id), [3, 1, 2]);
  assert.equal(out[0].nearest, true);
  assert.ok(out[0].km < 1 && out[2].km === null && !out[1].nearest);
  assert.equal(obras[0].km, undefined);                                    // no toca la lista original
  assert.deepEqual(sortByDistance(obras, null).map((x) => x.id), [1, 2, 3]);   // sin GPS, el orden de siempre
});

const OK = () => ({ author: 'Lucía', landmarkId: '7', catalog: { landmarks: [{ id: 7, name: 'Kenchō-ji' }] },
  fileCount: 1, head: jpegWithExif(), size: 3e6, usedToday: 0, bucketBytes: 0 });

test('la validación, en el orden en que se explica', () => {
  assert.equal(validateSubmission(OK()).ok, true);
  const cases = [
    [{ author: '   ' }, MESSAGES.author],
    [{ landmarkId: '' }, MESSAGES.landmark],
    [{ landmarkId: 'abc' }, MESSAGES.landmark],
    [{ catalog: null }, MESSAGES.noTrip],
    [{ landmarkId: '8' }, MESSAGES.notInCatalog],
    [{ fileCount: 0 }, MESSAGES.files],
    [{ fileCount: 2 }, MESSAGES.files],
    [{ head: new TextEncoder().encode('no soy una foto') }, MESSAGES.notImage],
    [{ head: HEIC }, MESSAGES.heic],
    [{ size: MAX_BYTES + 1 }, MESSAGES.tooBig],
    [{ usedToday: DAILY_PER_IP }, MESSAGES.quota],
    [{ bucketBytes: BUCKET_LIMIT - 1000, size: 2000 }, MESSAGES.full],
  ];
  for (const [change, msg] of cases) assert.equal(validateSubmission({ ...OK(), ...change }).error, msg, JSON.stringify(change));
  // si falla todo, gana lo primero de la lista
  assert.equal(validateSubmission({ ...OK(), author: '', landmarkId: '', fileCount: 3, size: MAX_BYTES * 2 }).error, MESSAGES.author);
});

test('la cuenta de envíos del día', () => {
  assert.deepEqual(quotaCheck(0), { ok: true, n: 1 });
  assert.deepEqual(quotaCheck(DAILY_PER_IP - 1), { ok: true, n: DAILY_PER_IP });
  assert.equal(quotaCheck(DAILY_PER_IP).ok, false);
  assert.equal(quotaCheck(undefined).ok, true);
});

test('el nombre, el secreto y la IP', async () => {
  assert.equal(cleanAuthor('  Lucía   García \n'), 'Lucía García');
  assert.equal(cleanAuthor('x'.repeat(200)).length, 60);
  assert.equal(sameSecret('abc', 'abc'), true);
  assert.equal(sameSecret('abd', 'abc'), false);
  assert.equal(sameSecret('ab', 'abc'), false);
  assert.equal(sameSecret('', ''), false);                                   // sin secreto configurado, nadie entra
  const a = await hashIp('1.2.3.4', 'sal'), b = await hashIp('1.2.3.4', 'otra');
  assert.match(a, /^[0-9a-f]{64}$/);
  assert.notEqual(a, b);
  assert.ok(!a.includes('1.2.3.4'));
});

test('el formulario lleva las funciones que necesita y nada que lo deje subir varias', () => {
  const html = formPage('japon-2026');
  assert.ok(html.includes('function readExif') && html.includes('function sortByDistance') && html.includes('function distanceKm'));
  assert.ok(!/multiple/.test(html) && !/capture=/.test(html));
  assert.ok(html.includes('"japon-2026"') && html.includes('localStorage'));
  assert.ok(html.trimEnd().endsWith('</html>') && html.includes('</script>'));
});

// ------------------------------------------------------------ el Worker entero

let sqlite = null;
try { sqlite = await import('node:sqlite'); } catch (e) { /* Node sin SQLite: se saltan */ }

// D1 sobre SQLite de verdad: prepare/bind/first/run/all/batch, con el mismo esquema.sql.
function fakeD1() {
  const db = new sqlite.DatabaseSync(':memory:');
  db.exec(readFileSync(new URL('./esquema.sql', import.meta.url), 'utf8'));
  const stmt = (sql, args = []) => ({
    bind: (...a) => stmt(sql, a),
    first: async () => db.prepare(sql).get(...args) ?? null,
    run: async () => { db.prepare(sql).run(...args); return { success: true }; },
    all: async () => ({ results: db.prepare(sql).all(...args) }),
  });
  return { raw: db, prepare: (sql) => stmt(sql), batch: async (list) => { for (const s of list) await s.run(); } };
}
function fakeR2() {
  const store = new Map();
  return {
    store,
    put: async (key, data, opts) => { store.set(key, { data: new Uint8Array(data), type: opts?.httpMetadata?.contentType }); },
    get: async (key) => (store.has(key) ? { body: store.get(key).data, httpMetadata: { contentType: store.get(key).type } } : null),
    delete: async (keys) => { for (const k of [].concat(keys)) store.delete(k); },
  };
}
const SECRET = 'secreto-de-prueba';
const CATALOG = { trip_name: 'Japón 2026', landmarks: [
  { id: 7, name: 'Kenchō-ji', architect: '—', city: 'Kamakura', lat: 35.33, lon: 139.55 },
  { id: 9, name: 'Museo Nezu', architect: 'Kengo Kuma', city: 'Tokio', lat: 35.66, lon: 139.72 },
] };
function setup() {
  const env = { BUZON: fakeD1(), FOTOS: fakeR2(), BUZON_SECRET: SECRET };
  const call = (path, init = {}) => worker.fetch(new Request('https://buzon.example' + path, init), env);
  const asPanda = (path, init = {}) => call(path, { ...init, headers: { ...(init.headers || {}), Authorization: 'Bearer ' + SECRET } });
  const send = (fields, files = [], ip = '1.2.3.4') => {
    const fd = new FormData();
    for (const [k, v] of Object.entries(fields)) fd.append(k, v);
    for (const f of files) fd.append('photo', new File([f], 'foto.jpg', { type: 'image/jpeg' }));
    return call('/japon-2026/subir', { method: 'POST', body: fd, headers: { 'CF-Connecting-IP': ip } });
  };
  return { env, call, asPanda, send };
}
const skip = !sqlite && 'node:sqlite no está (hace falta Node 22)';

test('panda deja el catálogo, el alumno sube y panda recoge', { skip }, async () => {
  const { env, call, asPanda, send } = setup();
  assert.equal((await call('/japon-2026')).status, 404);                     // sin catálogo no hay buzón
  assert.equal((await call('/panda/catalogo/japon-2026', { method: 'PUT', body: JSON.stringify(CATALOG) })).status, 401);
  let r = await asPanda('/panda/catalogo/japon-2026', { method: 'PUT', body: JSON.stringify(CATALOG) });
  assert.deepEqual(await r.json(), { ok: true, landmarks: 2 });

  r = await call('/japon-2026');
  assert.equal(r.status, 200);
  assert.match(r.headers.get('content-type'), /text\/html/);
  assert.equal((await (await call('/japon-2026/obras')).json()).landmarks.length, 2);

  const photo = jpegWithExif({ lat: 35.66, lon: 139.72 });
  r = await send({ author: '  Lucía  ', landmark_id: '9' }, [photo]);
  const { ok, id } = await r.json();
  assert.ok(ok && id);
  assert.equal(env.FOTOS.store.size, 1);

  const pend = (await (await asPanda('/panda/pendientes')).json()).items;
  assert.equal(pend.length, 1);
  assert.deepEqual([pend[0].author, pend[0].landmark_id, pend[0].landmark_name, pend[0].mime, pend[0].bytes],
    ['Lucía', 9, 'Museo Nezu', 'image/jpeg', photo.length]);
  assert.equal(pend[0].taken_at, '2026-04-12T10:31:05');
  assert.ok(Math.abs(pend[0].lat - 35.66) < 1e-4);
  assert.equal(pend[0].ip_hash, undefined);                                   // a panda no le hace falta

  r = await asPanda('/panda/objeto/' + pend[0].object_key);
  assert.deepEqual(new Uint8Array(await r.arrayBuffer()), photo);
  assert.equal(r.headers.get('content-type'), 'image/jpeg');

  const estado = await (await asPanda('/panda/estado')).json();
  assert.deepEqual([estado.nuevos, estado.bytes, estado.cuota.envios, estado.cuota.ips], [1, photo.length, 1, 1]);

  r = await asPanda('/panda/recogido', { method: 'POST', body: JSON.stringify({ ids: [id, 'no-existe'] }) });
  assert.deepEqual(await r.json(), { done: 1 });
  assert.equal(env.FOTOS.store.size, 0);                                       // el cubo no acumula
  assert.equal((await (await asPanda('/panda/pendientes')).json()).items.length, 0);
  r = await asPanda('/panda/recogido', { method: 'POST', body: JSON.stringify({ ids: [id] }) });
  assert.deepEqual(await r.json(), { done: 0 });                              // recoger dos veces no hace nada
  const ip = env.BUZON.raw.prepare('SELECT ip_hash FROM submissions').get().ip_hash;
  assert.ok(!ip.includes('1.2.3.4') && ip.length === 64);
});

test('el Worker rechaza con el mensaje que toca', { skip }, async () => {
  const { env, asPanda, send } = setup();
  await asPanda('/panda/catalogo/japon-2026', { method: 'PUT', body: JSON.stringify(CATALOG) });
  const err = async (r) => [r.status, (await r.json()).error];
  assert.deepEqual(await err(await send({ landmark_id: '9' }, [jpegWithExif()])), [400, MESSAGES.author]);
  assert.deepEqual(await err(await send({ author: 'Ana' }, [jpegWithExif()])), [400, MESSAGES.landmark]);
  assert.deepEqual(await err(await send({ author: 'Ana', landmark_id: '99' }, [jpegWithExif()])), [400, MESSAGES.notInCatalog]);
  assert.deepEqual(await err(await send({ author: 'Ana', landmark_id: '9' }, [jpegWithExif(), jpegWithExif()])), [400, MESSAGES.files]);
  assert.deepEqual(await err(await send({ author: 'Ana', landmark_id: '9' }, [new TextEncoder().encode('MZ ejecutable…')])), [415, MESSAGES.notImage]);
  assert.equal(env.FOTOS.store.size, 0);
  // 40 al día por conexión; otra conexión sigue pudiendo
  for (let i = 0; i < DAILY_PER_IP; i++) assert.equal((await send({ author: 'Ana', landmark_id: '9' }, [PNG], '5.5.5.5')).status, 200);
  assert.deepEqual(await err(await send({ author: 'Ana', landmark_id: '9' }, [PNG], '5.5.5.5')), [429, MESSAGES.quota]);
  assert.equal((await send({ author: 'Bea', landmark_id: '9' }, [PNG], '6.6.6.6')).status, 200);
  // el cubo lleno: el tope cuenta lo que espera a panda
  env.BUZON.raw.prepare("UPDATE submissions SET bytes = ? WHERE author = 'Bea'").run(BUCKET_LIMIT);
  assert.deepEqual(await err(await send({ author: 'Carla', landmark_id: '9' }, [PNG], '7.7.7.7')), [507, MESSAGES.full]);
  assert.equal((await asPanda('/panda/catalogo/panda', { method: 'PUT', body: JSON.stringify(CATALOG) })).status, 400);
});
