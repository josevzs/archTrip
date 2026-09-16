// Browser flow test over the DevTools protocol (no puppeteer): photo grid, ficha + keyboard, map, list, standalone export.
// Usage: node tests/browser/cdp.mjs [base-url] [trip-id]  — needs a running server and a trip with a few
// enriched landmarks (e.g. the Portugal set from README). Destructive: resets that trip's statuses and deletes one landmark.
import { spawn } from 'node:child_process';
import { writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PORT = 9335;
const ROOT = fileURLToPath(new URL('../..', import.meta.url)).replace(/[\/]$/, '');
const BASE = process.argv[2] || 'http://127.0.0.1:8000';
const TRIP = process.argv[3] || '1';
const profile = mkdtempSync(join(tmpdir(), 'archtrip-chrome-'));
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox', '--allow-file-access-from-files', '--window-size=1300,900',
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
async function key(k, code) { await send('Input.dispatchKeyEvent', { type: 'keyDown', key: k, code: code || k }); await send('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code: code || k }); await sleep(500); }
const consoleErrors = [];
let failures = 0;
function check(name, cond, extra = '') { console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '  ' + extra)); if (!cond) failures++; }
const text = (sel) => `(document.querySelector(${JSON.stringify(sel)})||{}).textContent||''`;
const count = (sel) => `document.querySelectorAll(${JSON.stringify(sel)}).length`;

try {
  await connect();
  await send('Runtime.enable'); await send('Page.enable');
  ws.addEventListener('message', (m) => {
    const msg = JSON.parse(m.data);
    if (msg.method === 'Runtime.exceptionThrown') consoleErrors.push(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text);
    if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') consoleErrors.push(msg.params.args.map((a) => a.value || a.description).join(' '));
  });
  const ids = (await (await fetch(`${BASE}/api/trips/${TRIP}`)).json()).landmarks.map((l) => l.id);
  for (const id of ids) await fetch(`${BASE}/api/landmarks/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: 'pendiente' }) });

  // ---- new-trip screen: AI-assisted path, then create and delete a trip
  await goto(`${BASE}/#/`);
  check('trips list offers new trip', await evaluate(`!!document.querySelector('a[href="#/nuevo"]')`));
  await goto(`${BASE}/#/nuevo`, 800);
  check('two creation paths', await evaluate(count('.choice')) === 2);
  await evaluate(`document.querySelector('[data-action="new-mode"][data-mode="ia"]').click()`); await sleep(1500);
  check('AI path shows templates, bold instructions and the prompt', await evaluate(count('.steps a[href*="templates/"]')) >= 3
    && (await evaluate(text('.steps b'))).includes('LOS DOS archivos') && (await evaluate(text('#promptbox'))).includes('plantilla_hitos.xlsx'));
  await evaluate(`document.querySelector('[data-form="create-trip"] input').value = 'Italia 2028';
                  document.querySelector('[data-form="create-trip"]').requestSubmit();`);
  await sleep(1200);
  check('create trip navigates to it', (await evaluate('location.hash')).startsWith('#/viaje/'), await evaluate('location.hash'));
  const newTripId = Number((await evaluate('location.hash')).split('/').pop());
  await fetch(`${BASE}/api/trips/${newTripId}`, { method: 'DELETE' });
  await goto(`${BASE}/#/nuevo/manual`, 800);
  check('manual path shows a plain create form', await evaluate(`!!document.querySelector('.steps [data-form="create-trip"]')`) && !(await evaluate(`!!document.getElementById('promptbox')`)));
  await goto(`${BASE}/#/viaje/${TRIP}`, 2500);

  // ---- fotos (default view)
  await goto(`${BASE}/#/viaje/${TRIP}`, 2500);
  await evaluate(`localStorage.removeItem('archtrip:view')`);
  await evaluate(`document.querySelector('[data-action="view"][data-view="fotos"]').click()`); await sleep(400);
  check('fotos view shows cards', await evaluate(count('.card')) === ids.length, await evaluate(count('.card')));
  check('cards carry photos', await evaluate(count('.card .pic img')) >= 2);
  check('badge counts pictures', /\d+ fotos/.test(await evaluate(text('.card .badge'))), await evaluate(text('.card .badge')));

  // ---- ficha + keyboard
  await evaluate(`document.querySelector('.card .pic').click()`); await sleep(500);
  check('ficha opens', await evaluate(`!!document.getElementById('detail')`));
  check('ficha shows a large image', await evaluate(`!!document.querySelector('#detail .stage img')`));
  check('strip has photos', await evaluate(count('#detail .strip img')) >= 2);
  check('position counter', /^1 \/ \d+/.test(await evaluate(text('#detail .dhead .muted'))), await evaluate(text('#detail .dhead .muted')));
  const firstName = await evaluate(text('#detail .dside .name'));
  await key('ArrowDown'); check('arrow down changes image', await evaluate(`document.querySelectorAll('#detail .strip img.on')[0] !== document.querySelector('#detail .strip img')`));
  await key('ArrowRight'); check('arrow right moves to next landmark', (await evaluate(text('#detail .dside .name'))) !== firstName);
  await key('ArrowLeft'); check('arrow left goes back', (await evaluate(text('#detail .dside .name'))) === firstName);
  await key('c', 'KeyC');
  const afterC = await evaluate(text('#detail .dside .name'));
  check('C curates and advances', afterC !== firstName, afterC);
  const st = (await (await fetch(`${BASE}/api/trips/${TRIP}`)).json()).landmarks;
  check('status persisted via keyboard', st.some((l) => l.status === 'curado'));
  await key('x', 'KeyX');
  check('X discards and advances', (await evaluate(text('#detail .dside .name'))) !== afterC);
  await key('Escape');
  check('Esc closes ficha', !(await evaluate(`!!document.getElementById('detail')`)));
  check('rejected card hidden by default', await evaluate(count('.card.curado')) === 1 && await evaluate(count('.card.descartado')) === 0
    && await evaluate(`document.querySelector('[data-toggle="hideRejected"]').checked`));
  check('tabs updated', (await evaluate(text('#tabs'))).includes('Curados 1') && (await evaluate(text('#tabs'))).includes('Descartados 1'));
  await evaluate(`document.querySelector('[data-action="filter"][data-filter="descartado"]').click()`); await sleep(300);
  check('trash only on rejected cards', await evaluate(count('.card.descartado .trash')) === 1 && await evaluate(count('.card .trash')) === 1);
  await evaluate(`document.querySelector('[data-action="filter"][data-filter="todos"]').click()`); await sleep(300);
  await evaluate(`{ const t = document.querySelector('[data-toggle="hideRejected"]'); t.checked = false; t.dispatchEvent(new Event('change', {bubbles:true})); }`); await sleep(300);
  check('unhiding shows rejected card', await evaluate(count('.card.descartado')) === 1 && await evaluate(count('.card .trash')) === 1);
  await evaluate(`document.querySelector('.card .pic').click()`); await sleep(400);
  const skipOn = await evaluate(`document.querySelector('[data-toggle="skipRejected"]').checked`);
  const totalShown = await evaluate(text('#detail .dhead .muted'));
  check('ficha skips rejected by default', skipOn && totalShown.endsWith('/ ' + (ids.length - 1)), totalShown);
  await evaluate(`{ const t = document.querySelector('[data-toggle="skipRejected"]'); t.checked = false; t.dispatchEvent(new Event('change', {bubbles:true})); }`); await sleep(300);
  check('unskipping counts them again', (await evaluate(text('#detail .dhead .muted'))).endsWith('/ ' + ids.length));
  check('google buttons and maps links', await evaluate(count('#detail .bigbtns a')) === 2 && (await evaluate(text('#detail .links'))).includes('Google Maps'));
  await key('Escape');
  await evaluate(`{ const t = document.querySelector('[data-toggle="hideRejected"]'); t.checked = true; t.dispatchEvent(new Event('change', {bubbles:true})); }`); await sleep(300);

  // ---- arquitectos view
  await evaluate(`document.querySelector('[data-action="view"][data-view="arquitectos"]').click()`); await sleep(400);
  check('arquitectos view groups by architect', (await evaluate(text('.group-head'))).toUpperCase().includes('SIZA') || (await evaluate(text('.group-head'))).includes('Koolhaas'));
  check('arquitectos view shows cards', await evaluate(count('.card')) === ids.length - 1);
  await evaluate(`document.querySelector('[data-action="view"][data-view="fotos"]').click()`); await sleep(300);

  // filter to pendientes, curate through the ficha until the list empties
  await evaluate(`document.querySelector('[data-action="filter"][data-filter="pendiente"]').click()`); await sleep(300);
  const pendingLeft = ids.length - 2;
  check('pendientes filter', await evaluate(count('.card')) === pendingLeft, await evaluate(count('.card')));
  await evaluate(`document.querySelector('.card .pic').click()`); await sleep(400);
  for (let i = 0; i < pendingLeft; i++) await key('p', 'KeyP');
  check('last pending curated closes the ficha', !(await evaluate(`!!document.getElementById('detail')`)) && (await evaluate(text('#list'))).includes('Nada que mostrar'));
  await evaluate(`document.querySelector('[data-action="filter"][data-filter="todos"]').click()`); await sleep(300);

  // ---- mapa
  await evaluate(`document.querySelector('[data-action="view"][data-view="mapa"]').click()`); await sleep(4000);
  check('leaflet loaded', await evaluate(`!!window.L && !!document.querySelector('.leaflet-container')`));
  check('markers drawn', await evaluate(count('.leaflet-interactive')) >= ids.length + 1);   // circles + route line
  check('stop icons drawn', await evaluate(count('.stopicon')) === 2);
  check('legend mentions count', (await evaluate(text('#map-note'))).includes('hitos en el mapa'));
  check('basemap selector offers providers', await evaluate(count('.leaflet-control-layers-base input')) >= 8);
  await evaluate(`Array.from(document.querySelectorAll('.leaflet-control-layers-base label')).find(l => l.textContent.includes('GSI estándar')).querySelector('input').click()`); await sleep(1500);
  check('switching basemap loads other tiles', await evaluate(`Array.from(document.querySelectorAll('.leaflet-tile')).some(t => t.src.includes('cyberjapandata'))`));
  check('basemap choice remembered', (await evaluate(`localStorage.getItem('archtrip:basemap')`)) === 'Japón — GSI estándar');
  await evaluate(`document.querySelectorAll('path.leaflet-interactive')[1].dispatchEvent(new MouseEvent('click', {bubbles:true}))`); await sleep(500);
  check('popup opens with actions', await evaluate(count('.leaflet-popup [data-action="set-status"]')) === 3);
  await evaluate(`document.querySelector('.leaflet-popup [data-action="set-status"][data-status="posible"]').click()`); await sleep(600);
  check('status change from popup recolours marker', (await evaluate(`Array.from(document.querySelectorAll('path.leaflet-interactive')).map(p => p.getAttribute('stroke')).join(',')`)).includes('#b8860b'));

  // ---- ficha: add a picture by URL, remove it, delete a landmark with Supr
  await evaluate(`document.querySelector('[data-action="view"][data-view="fotos"]').click()`); await sleep(400);
  await evaluate(`document.querySelector('.card .pic').click()`); await sleep(400);
  const beforePics = await evaluate(count('#detail .strip img'));
  await evaluate(`document.querySelector('[data-action="add-image-form"]').click()`); await sleep(300);
  check('add-image form opens', await evaluate(`!!document.querySelector('[data-form="add-image"]')`));
  await evaluate(`{ const f = document.querySelector('[data-form="add-image"]'); f.url.value = 'https://upload.wikimedia.org/wikipedia/commons/thumb/4/4f/Casa_da_Musica_Porto.jpg/640px-Casa_da_Musica_Porto.jpg'; f.kind.value = 'plano'; f.requestSubmit(); }`); await sleep(1200);
  check('picture added by URL', await evaluate(count('#detail .strip img')) === beforePics + 1);
  check('added picture is shown first and marked manual', (await evaluate(text('#detail .caption'))).includes('640px-Casa_da_Musica_Porto.jpg'));
  const orderBefore = await evaluate(`Array.from(document.querySelectorAll('#detail .strip img[draggable]')).map(i => i.dataset.img)`);
  await evaluate(`{ const imgs = document.querySelectorAll('#detail .strip img[draggable]'); const dt = new DataTransfer();
    const a = imgs[0], b = imgs[1];   // two photos: kinds are ordered separately
    a.dispatchEvent(new DragEvent('dragstart', {bubbles:true, dataTransfer: dt}));
    b.dispatchEvent(new DragEvent('dragover', {bubbles:true, cancelable:true, dataTransfer: dt}));
    b.dispatchEvent(new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer: dt}));
    a.dispatchEvent(new DragEvent('dragend', {bubbles:true, dataTransfer: dt})); }`); await sleep(900);
  const orderAfter = await evaluate(`Array.from(document.querySelectorAll('#detail .strip img[draggable]')).map(i => i.dataset.img)`);
  check('drag reorders thumbnails', orderBefore.length === orderAfter.length && orderAfter[1] === orderBefore[0] && orderAfter[0] === orderBefore[1],
    orderBefore.join() + ' -> ' + orderAfter.join());
  const apiOrder = (await (await fetch(`${BASE}/api/trips/${TRIP}`)).json()).landmarks.find((l) => l.images.length === orderAfter.length).images.map((i) => String(i.id));
  check('order persisted in API', apiOrder.join() === orderAfter.join(), apiOrder.join());
  const curImg = Number(await evaluate(`document.querySelector('#detail .caption [data-action="set-kind"]').dataset.img`));
  const kindBefore = (await (await fetch(`${BASE}/api/trips/${TRIP}`)).json()).landmarks.flatMap((l) => l.images).find((i) => i.id === curImg).kind;
  await evaluate(`document.querySelector('#detail .caption [data-action="set-kind"]').click()`); await sleep(800);
  const kindAfter = (await (await fetch(`${BASE}/api/trips/${TRIP}`)).json()).landmarks.flatMap((l) => l.images).find((i) => i.id === curImg).kind;
  check('reclassify toggles kind', kindBefore !== kindAfter, kindBefore + ' -> ' + kindAfter);
  check('same picture still shown after reclassifying', Number(await evaluate(`document.querySelector('#detail .caption [data-action="set-kind"]').dataset.img`)) === curImg);
  await evaluate(`document.querySelector('[data-action="delete-image"]').click()`); await sleep(800);
  check('picture removed again', await evaluate(count('#detail .strip img')) === beforePics);
  await evaluate(`window.confirm = () => true`);
  const victim = await evaluate(text('#detail .dside .name'));
  const total = await evaluate(text('#detail .dhead .muted'));
  await key('Delete');
  check('Supr deletes and moves on', (await evaluate(text('#detail .dside .name'))) !== victim && (await evaluate(text('#detail .dhead .muted'))) !== total);
  const left = (await (await fetch(`${BASE}/api/trips/${TRIP}`)).json()).landmarks;
  check('landmark gone from API', left.length === ids.length - 1 && !left.some((l) => l.name === victim));
  await key('Escape');
  check('grid shrank', await evaluate(count('.card')) === ids.length - 2);   // one deleted, one rejected (hidden)

  // ---- lista (legacy checks)
  await evaluate(`document.querySelector('[data-action="view"][data-view="lista"]').click()`); await sleep(400);
  check('lista view rows', await evaluate(count('.lm')) === ids.length - 2);
  check('list thumbnails from fetched photos', await evaluate(count('.lm .thumb img')) >= 1);
  check('eliminar always offered in list', await evaluate(count('.lm [data-action="delete-landmark"]')) === ids.length - 2);
  check('wikipedia link present', (await evaluate(text('.lm .links'))).includes('Wikipedia'));
  await evaluate(`document.querySelector('.lm [data-action="edit"]').click()`); await sleep(300);
  check('edit form opens in list', await evaluate(`!!document.querySelector('.lm form.edit')`));
  await evaluate(`document.querySelector('[data-action="cancel-edit"]').click()`); await sleep(200);
  await evaluate(`{ const s = document.getElementById('search'); s.value = 'siza'; s.dispatchEvent(new Event('input', {bubbles:true})); }`); await sleep(300);
  const sizaCount = left.filter((l) => l.status !== 'descartado' && (l.name + ' ' + l.architect + ' ' + l.city).toLowerCase().includes('siza')).length;
  check('search filters', await evaluate(count('.lm')) === sizaCount, await evaluate(count('.lm')));
  await evaluate(`{ const s = document.getElementById('search'); s.value = ''; s.dispatchEvent(new Event('input', {bubbles:true})); }`); await sleep(300);

  // ---- standalone export with images
  const html = await (await fetch(`${BASE}/api/trips/${TRIP}/export/html`)).text();
  writeFileSync(`${ROOT}/data/export.html`, html);
  await goto(`file:///${ROOT}/data/export.html`, 1500);
  await evaluate(`document.querySelector('[data-action="view"][data-view="fotos"]').click()`); await sleep(300);
  check('standalone cards with photos', await evaluate(count('.card .pic img')) >= 1);
  await evaluate(`document.querySelector('.card .pic').click()`); await sleep(400);
  check('standalone ficha', await evaluate(`!!document.querySelector('#detail .stage img')`));
  await key('c', 'KeyC'); await key('Escape');
  await goto(`file:///${ROOT}/data/export.html`, 1500);
  check('standalone keyboard edit persisted', await evaluate(count('.card.curado')) >= 1 || await evaluate(count('.lm.curado')) >= 1);
  await evaluate(`document.querySelector('[data-action="view"][data-view="mapa"]').click()`); await sleep(4000);
  check('standalone map works (CDN)', await evaluate(`!!document.querySelector('.leaflet-container')`));
  check('buildCopy still works', typeof (await evaluate(`archtrip.buildCopy()`)) === 'string');

  check('credits bar: name plus Ko-fi and GitHub icons', await evaluate(`!!document.querySelector('#credits a[href*="ko-fi.com/josevzs"] svg') && !!document.querySelector('#credits a[href*="github.com/josevzs/archTrip"] svg')`) && (await evaluate(text('#credits'))).includes('Vargas-Zúñiga'));
  check('no console errors', consoleErrors.length === 0, JSON.stringify(consoleErrors).slice(0, 600));
} catch (e) {
  console.log('ERROR', e.message); failures++;
} finally {
  try { ws?.close(); } catch {}
  chrome.kill();
  console.log(failures ? `\n${failures} FAILED` : '\nALL OK');
  process.exit(failures ? 1 : 0);
}
