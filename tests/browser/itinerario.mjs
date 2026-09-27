// Browser flow test for the itinerary view (#/viaje/<id>, pestaña Itinerario) over the DevTools protocol.
// Usage: node tests/browser/itinerario.mjs [base-url] [trip-id] — needs a running server and a trip with a
// route and a few landmarks. Destructive: creates and deletes days of that trip.
import { spawn } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PORT = 9338;
const BASE = process.argv[2] || 'http://127.0.0.1:8000';
const TRIP = process.argv[3] || '1';
const profile = mkdtempSync(join(tmpdir(), 'archtrip-chrome-'));
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox', '--window-size=1300,1000',
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
async function goto(url, wait = 2000) { await send('Page.navigate', { url }); await sleep(wait); }
// a native-like value change: set it and fire the event the app listens to
const setField = (sel, value, event = 'change') => evaluate(
  `(() => { const el = document.querySelector(${JSON.stringify(sel)}); el.value = ${JSON.stringify(value)};
            el.dispatchEvent(new Event(${JSON.stringify(event)}, { bubbles: true })); })()`);
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
  // limpia el itinerario que haya quedado de una ejecución anterior
  const existing = (await (await fetch(`${BASE}/api/trips/${TRIP}`)).json()).days || [];
  for (const d of existing) await fetch(`${BASE}/api/days/${d.id}`, { method: 'DELETE' });

  await goto(`${BASE}/#/viaje/${TRIP}`, 2500);
  check('la vista Itinerario está en la barra', await evaluate(`!!document.querySelector('[data-action="view"][data-view="itinerario"]')`));
  await evaluate(`document.querySelector('[data-action="view"][data-view="itinerario"]').click()`); await sleep(600);
  check('sin días, explica cómo empezar', (await evaluate(text('#list'))).includes('Añadir día'));
  check('las pestañas de curado se ocultan en esta vista', await evaluate(`getComputedStyle(document.getElementById('tabs')).display`) === 'none');

  // ---- crear días (los identificamos por id: al ponerles fecha se reordenan solos)
  await evaluate(`document.querySelector('[data-action="day-add"]').click()`); await sleep(700);
  await evaluate(`document.querySelector('[data-action="day-add"]').click()`); await sleep(700);
  check('dos días creados', await evaluate(count('.dayc')) === 2, await evaluate(count('.dayc')));
  const days = await evaluate(`Array.from(document.querySelectorAll('.dayc')).map(d => ({ id: d.dataset.day, date: d.querySelector('input[type=date]').value }))`);
  check('el segundo día encadena el día siguiente', new Date(days[1].date) - new Date(days[0].date) === 86400000, JSON.stringify(days));
  const D1 = `.dayc[data-day="${days[0].id}"]`, D2 = `.dayc[data-day="${days[1].id}"]`;

  await setField(`${D1} input[type=date]`, '2027-04-12'); await sleep(800);
  check('la fecha se guarda y se escribe en bonito', (await evaluate(text(`${D1} .when`))).includes('12 de abril'), await evaluate(text(`${D1} .when`)));
  check('el día con fecha posterior se coloca el último', (await evaluate(`document.querySelectorAll('.dayc')[1].dataset.day`)) === days[0].id);
  await setField(`${D1} input.title`, 'Oporto a pie'); await sleep(700);
  await setField(`${D1} select[data-day-field="stop_id"]`, await evaluate(`document.querySelectorAll('${D1} select[data-day-field="stop_id"] option')[1].value`)); await sleep(700);
  await setField(`${D1} input.daynotes`, 'recoger las llaves del piso'); await sleep(700);

  // ---- añadir hitos y una nota
  await evaluate(`document.querySelector('${D1} [data-action="picker-open"]').click()`); await sleep(500);
  check('el buscador de hitos se abre', await evaluate(`!!document.getElementById('picker-input')`));
  await setField('#picker-input', 'serralves', 'input'); await sleep(400);
  check('el buscador filtra', await evaluate(count('.picker .hit')) === 1, await evaluate(count('.picker .hit')));
  await evaluate(`document.querySelector('.picker .hit').click()`); await sleep(900);
  await evaluate(`document.querySelector('${D1} [data-action="picker-open"]').click()`); await sleep(500);
  await setField('#picker-input', 'música', 'input'); await sleep(400);
  await evaluate(`document.querySelector('.picker .hit').click()`); await sleep(900);
  await evaluate(`document.querySelector('${D1} [data-action="item-note"]').click()`); await sleep(900);
  check('dos hitos y una nota en ese día', await evaluate(count(`${D1} li[data-item]`)) === 3, await evaluate(count(`${D1} li[data-item]`)));
  check('el hito muestra arquitecto y enlace a su ficha',
    (await evaluate(`Array.from(document.querySelectorAll('${D1} li .body .arch')).map(e => e.textContent.toUpperCase()).join('|')`)).includes('SIZA')
    && await evaluate(count(`${D1} li .body a[href*="/hito/"]`)) === 2);
  await evaluate(`document.querySelector('${D1} [data-action="picker-open"]').click()`); await sleep(700);
  check('el buscador avisa de los hitos que ya están puestos', (await evaluate(text('.picker'))).includes('ya está en el día'));
  await evaluate(`document.querySelector('[data-action="picker-close"]').click()`); await sleep(400);

  // ---- horas y orden
  await setField(`${D1} li:nth-child(1) input[type=time]`, '16:00'); await sleep(700);
  await setField(`${D1} li:nth-child(2) input[type=time]`, '09:30'); await sleep(700);
  check('aparece el botón de ordenar por hora', await evaluate(`!!document.querySelector('${D1} [data-action="sort-by-time"]')`));
  await evaluate(`document.querySelector('${D1} [data-action="sort-by-time"]').click()`); await sleep(900);
  const times = await evaluate(`Array.from(document.querySelectorAll('${D1} li input[type=time]')).map(i => i.value)`);
  check('ordenar por hora deja 09:30 antes que 16:00', times[0] === '09:30' && times[1] === '16:00', JSON.stringify(times));

  // ---- nota editable
  await setField(`${D1} textarea.note`, 'comida por la Ribeira'); await sleep(800);
  await goto(`${BASE}/#/viaje/${TRIP}`, 2500);
  await evaluate(`document.querySelector('[data-action="view"][data-view="itinerario"]').click()`); await sleep(700);
  check('todo sobrevive a recargar', (await evaluate(text('#list'))).includes('comida por la Ribeira')
    && (await evaluate(`document.querySelector('${D1} input.title').value`)) === 'Oporto a pie'
    && (await evaluate(`document.querySelector('${D1} input.daynotes').value`)) === 'recoger las llaves del piso');

  // ---- arrastrar: reordenar dentro del día y mover a otro día
  const drag = (fromSel, toSel) => evaluate(`(() => {
      const dt = new DataTransfer();
      const a = document.querySelector(${JSON.stringify(fromSel)}), b = document.querySelector(${JSON.stringify(toSel)});
      a.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
      b.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt }));
      a.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
    })()`);
  const order = () => evaluate(`Array.from(document.querySelectorAll('${D1} li[data-item]')).map(li => li.dataset.item)`);
  const before = await order();
  await drag(`${D1} li[data-item="${before[2]}"]`, `${D1} li[data-item="${before[0]}"]`); await sleep(1200);
  const after = await order();
  check('arrastrar reordena dentro del día', JSON.stringify(after) === JSON.stringify([before[2], before[0], before[1]]),
    JSON.stringify(after) + ' esperado ' + JSON.stringify([before[2], before[0], before[1]]));
  await drag(`${D1} li[data-item="${before[2]}"]`, `${D2} ul`); await sleep(1400);
  check('arrastrar a otro día lo mueve', await evaluate(count(`${D2} li[data-item]`)) === 1
    && await evaluate(count(`${D1} li[data-item]`)) === 2,
    (await evaluate(count(`${D2} li[data-item]`))) + '/' + (await evaluate(count(`${D1} li[data-item]`))));
  const server = (await (await fetch(`${BASE}/api/trips/${TRIP}`)).json()).days.find((d) => String(d.id) === days[1].id);
  check('el cambio de día está guardado en el servidor', server.items.length === 1, JSON.stringify(server.items));

  // ---- desde la ficha
  await evaluate(`document.querySelector('[data-action="view"][data-view="fotos"]').click()`); await sleep(700);
  await evaluate(`document.querySelector('.card [data-action="open"]').click()`); await sleep(1300);
  check('la ficha ofrece añadir a un día', await evaluate(`!!document.querySelector('select[data-add-day]')`));
  await setField('select[data-add-day]', days[1].id); await sleep(1400);
  check('la ficha dice en qué día está el hito', (await evaluate(text('#detail .dside'))).includes('En el itinerario: día'),
    (await evaluate(text('#detail .dside'))).slice(0, 140));

  // ---- export
  const html = await (await fetch(`${BASE}/api/trips/${TRIP}/export/itinerario`)).text();
  check('el itinerario descargable trae días, horas, notas y base', html.includes('lunes 12 de abril de 2027')
    && html.includes('09:30') && html.includes('comida por la Ribeira') && html.includes('Base: Oporto') && html.includes('Oporto a pie'),
    html.slice(0, 200));

  // ---- borrar un día
  await goto(`${BASE}/#/viaje/${TRIP}`, 2500);
  await evaluate(`document.querySelector('[data-action="view"][data-view="itinerario"]').click()`); await sleep(700);
  await evaluate(`window.confirm = () => true; document.querySelector('${D1} [data-action="day-delete"]').click()`); await sleep(1400);
  check('el día se borra y los hitos siguen en el viaje', await evaluate(count('.dayc')) === 1
    && (await (await fetch(`${BASE}/api/trips/${TRIP}`)).json()).landmarks.length === 3, await evaluate(count('.dayc')));

  check('sin errores de consola', consoleErrors.length === 0, JSON.stringify(consoleErrors).slice(0, 600));
} catch (e) {
  console.log('ERROR', e.message); failures++;
} finally {
  try { ws?.close(); } catch {}
  chrome.kill();
  console.log(failures ? `\n${failures} FAILED` : '\nALL OK');
  process.exit(failures ? 1 : 0);
}
