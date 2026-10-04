// Browser flow for the "Descubrir" view: search, zone tabs, pick + import, and the envelope /
// corridor drawn on the map. Usage: node tests/browser/descubrir.mjs [base-url] [trip-id]
// Needs a running server and a trip whose route is already located (the two Portugal stops are
// enough). It only asks Arquitectura Viva (fast and keyless) so the run stays under a few seconds.
// Destructive: it imports one candidate into that trip as "posible".
import { spawn } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PORT = 9337;
const BASE = process.argv[2] || 'http://127.0.0.1:8000';
const TRIP = process.argv[3] || '1';
const profile = mkdtempSync(join(tmpdir(), 'archtrip-chrome-'));
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox', '--window-size=1300,950',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ws, seq = 0; const pending = new Map();
async function connect() {
  for (let i = 0; i < 50; i++) {
    try {
      const tabs = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
      ws = new WebSocket(tabs.find((t) => t.type === 'page').webSocketDebuggerUrl);
      await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
      ws.onmessage = (m) => { const msg = JSON.parse(m.data); if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); } };
      return;
    } catch (e) { await sleep(200); }
  }
  throw new Error('chrome did not come up');
}
const send = (method, params = {}) => { const id = ++seq; ws.send(JSON.stringify({ id, method, params })); return new Promise((res) => pending.set(id, res)); };
async function evaluate(expr) {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.result.exceptionDetails) throw new Error('JS: ' + JSON.stringify(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text));
  return r.result.result.value;
}
async function goto(url, wait = 1500) { await send('Page.navigate', { url }); await sleep(wait); }
async function waitFor(expr, ms = 60000) {
  for (let t = 0; t < ms; t += 300) { if (await evaluate(expr)) return true; await sleep(300); }
  return false;
}
const consoleErrors = [];
let failures = 0;
function check(name, cond, extra = '') { console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '  ' + extra)); if (!cond) failures++; }
const count = (sel) => `document.querySelectorAll(${JSON.stringify(sel)}).length`;
const click = (sel) => `(function(){const e=document.querySelector(${JSON.stringify(sel)});if(!e)return false;e.click();return true}())`;

try {
  await connect();
  await send('Runtime.enable'); await send('Page.enable');
  ws.addEventListener('message', (m) => {
    const msg = JSON.parse(m.data);
    if (msg.method === 'Runtime.exceptionThrown') consoleErrors.push(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text);
    if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') consoleErrors.push(msg.params.args.map((a) => a.value || a.description).join(' '));
  });

  const before = (await (await fetch(`${BASE}/api/trips/${TRIP}`)).json()).landmarks.length;
  await goto(`${BASE}/#/viaje/${TRIP}`, 2000);
  check('the Descubrir view is offered', await evaluate(`!!document.querySelector('[data-action="view"][data-view="descubrir"]')`));
  await evaluate(click('[data-action="view"][data-view="descubrir"]'));
  await sleep(400);
  check('it explains the envelope and the corridor', /envolvente/.test(await evaluate(`document.getElementById('list').textContent`)));
  check('the three sources are listed', await evaluate(count('[data-action="disc-source"]')) === 3);
  check('filters and tabs are hidden in this view', await evaluate(`getComputedStyle(document.getElementById('tabs')).display === 'none'`));

  // solo Arquitectura Viva: rápido y sin esperar a Wikidata
  check('Wikidata comes switched off', await evaluate(`document.querySelector('[data-action="disc-source"][data-src="wikidata"]').checked === false`));
  await evaluate(click('[data-action="disc-source"][data-src="iwanbaan"]'));
  await evaluate(click('[data-action="disc-run"]'));
  check('it says it is asking the sources', await waitFor(`/Preguntando/.test(document.getElementById('list').textContent)`, 3000));
  check('candidates come back', await waitFor(`${count('.cand')} > 0`, 90000));
  const cands = await evaluate(count('.cand'));
  console.log('     candidatos:', cands);
  check('each candidate shows its architect and zone', await evaluate(count('.cand .arch')) === cands && await evaluate(count('.cand .zone')) === cands);
  check('each candidate links to its source', await evaluate(count('.cand .srcs a')) > 0);
  check('the import button starts disabled', await evaluate(`document.querySelector('[data-action="disc-import"]').disabled === true`));

  await evaluate(click('[data-action="disc-all"]'));
  await sleep(300);
  check('marking all enables the import button', await evaluate(`document.querySelector('[data-action="disc-import"]').disabled === false`));
  check('the button says how many', /Importar \d+/.test(await evaluate(`document.querySelector('[data-action="disc-import"]').textContent`)));
  await evaluate(click('[data-action="disc-none"]'));
  await sleep(300);
  check('«ninguna» clears the selection', await evaluate(`document.querySelector('[data-action="disc-import"]').disabled === true`));

  // una sola, y a importar
  const chosen = await evaluate(`(function(){const c=document.querySelector('.cand');c.querySelector('input').click();return c.querySelector('.name').textContent}())`);
  await sleep(300);
  check('picking one highlights the card', await evaluate(count('.cand.on')) === 1);
  await evaluate(click('[data-action="disc-import"]'));
  check('it reports the import', await waitFor(`/importado/.test(document.getElementById('msg').textContent)`, 30000), chosen);
  const after = await (await fetch(`${BASE}/api/trips/${TRIP}`)).json();
  const added = after.landmarks.find((l) => l.name === chosen);
  check('the landmark is in the trip as «posible»', !!added && added.status === 'posible', chosen);
  check('with the Arquitectura Viva page on it', !!added && /arquitecturaviva\.com\/obras\//.test(added.url_av || ''));
  check('and one landmark more than before', after.landmarks.length === before + 1);
  check('the imported one is gone from the candidate list', await evaluate(count('.cand')) === cands - 1);

  // ---- el mapa: envolvente, corredor y candidatos
  await evaluate(click('[data-action="view"][data-view="mapa"]'));
  check('the map draws the candidates', await waitFor(`${count('.candico')} > 0`, 30000));
  check('and the envelope as circles', await evaluate(count('#map path.leaflet-interactive, #map svg path')) > 0);
  check('the legend can switch the zone off', await evaluate(`!!document.querySelector('[data-action="disc-zone-map"]')`));

  // importar desde el mapa no debe reconstruir el mapa: ni el encuadre ni el zoom se mueven
  await evaluate(`document.getElementById('map').dataset.sentinel = 'vivo'`);
  // la posición en pantalla de la parada 1: si el mapa no se mueve, no cambia
  const anchor = `((document.querySelector('.stopicon')||{}).parentElement||{style:{}}).style.transform || ''`;
  const pane = await evaluate(anchor);
  const left = await evaluate(count('.candico'));
  await evaluate(click('.candico'));
  await sleep(700);
  check('a candidate on the map opens its popup', await evaluate(`!!document.querySelector('[data-action="disc-one"]')`));
  await evaluate(click('[data-action="disc-one"]'));
  check('importing from the map reports it', await waitFor(`/importado/.test(document.getElementById('msg').textContent)`, 30000));
  check('the map was not rebuilt', await evaluate(`(document.getElementById('map')||{dataset:{}}).dataset.sentinel === 'vivo'`));
  check('and it did not move', await evaluate(anchor) === pane, pane);
  check('one candidate less on the map', await waitFor(`${count('.candico')} === ${left - 1}`, 10000));

  // ---- el mapa con las fotos encima
  check('the legend offers the photo mode', await evaluate(`!!document.querySelector('[data-action="map-photos"]')`));
  await evaluate(click('[data-action="map-photos"]'));
  await sleep(1200);
  check('photos (or photo groups) are drawn on the points',
    await evaluate(count('.phico')) + await evaluate(count('.phcluster')) > 0);
  await evaluate(click('[data-action="map-photos"]'));
  await sleep(800);
  check('switching it off goes back to the dots', await evaluate(count('.phico')) + await evaluate(count('.phcluster')) === 0);

  await evaluate(click('[data-action="disc-zone-map"]'));
  await sleep(500);
  check('switching the zone off clears the layer', await evaluate(count('.candico')) === 0);

  // ---- la ficha: pescar una foto para un hito que no tiene ninguna
  const all = (await (await fetch(`${BASE}/api/trips/${TRIP}`)).json()).landmarks;
  let victim = all.find((l) => !l.images.length) || all.find((l) => l.images.length);
  for (const im of victim.images || []) {          // se le quitan todas, para probar el pescador
    await fetch(`${BASE}/api/landmarks/${victim.id}/images/${im.id}`, { method: 'DELETE' });
  }
  const fresh = (await (await fetch(`${BASE}/api/trips/${TRIP}`)).json()).landmarks.find((l) => l.id === victim.id);
  check('the landmark starts with no pictures', fresh.images.length === 0, victim.name);
  // cambiar el hash no recarga: hay que releer el viaje para que la ficha vea que ya no hay foto
  await send('Page.navigate', { url: `${BASE}/#/viaje/${TRIP}/hito/${victim.id}` });
  await sleep(300);
  await send('Page.reload', { ignoreCache: true });
  await sleep(2500);
  check('the ficha opens', await waitFor(`!!document.getElementById('detail')`, 20000), victim.name);
  check('and offers to fish a photo from the web',
    await waitFor(`!!document.querySelector('[data-action="web-image"]')`, 10000), victim.name);
  await evaluate(click('[data-action="web-image"]'));
  check('it finishes and the landmark ends up with a picture',
    await waitFor(`!document.querySelector('[data-action="web-image"]')`, 120000));
  const fished = (await (await fetch(`${BASE}/api/trips/${TRIP}`)).json()).landmarks.find((l) => l.id === victim.id);
  check('saved with the source it came from', fished.images.length === 1, JSON.stringify(fished.images));
  console.log('     foto pescada en:', (fished.images[0] || {}).source || 'ninguna');

  check('no console errors', consoleErrors.length === 0, consoleErrors.join(' | '));
} catch (e) {
  console.log('FAIL exception  ' + e.message);
  failures++;
} finally {
  try { ws && ws.close(); } catch (e) { /* ignore */ }
  chrome.kill();
  console.log(failures ? `\n${failures} comprobaciones han fallado` : '\nTodo en verde');
  process.exit(failures ? 1 : 0);
}
