// Browser flow test for the hidden admin page (#/admin) over the DevTools protocol (no puppeteer).
// Usage: node tests/browser/admin.mjs [base-url] [trip-id]  — needs a running server whose journal already
// holds a session with a status change, a deleted landmark and a landmark named "Casa da Música"
// (e.g. the trip seeded by tests/test_admin.py's `seed`, or any curated trip). Destructive: undoes
// that session's changes and creates a backup file.
import { spawn } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PORT = 9336;
const BASE = process.argv[2] || 'http://127.0.0.1:8000';
const profile = mkdtempSync(join(tmpdir(), 'archtrip-chrome-'));
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox', '--window-size=1300,900',
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
const consoleErrors = [];
let failures = 0;
function check(name, cond, extra = '') { console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '  ' + extra)); if (!cond) failures++; }
const text = (sel) => `(document.querySelector(${JSON.stringify(sel)})||{}).textContent||''`;
const count = (sel) => `document.querySelectorAll(${JSON.stringify(sel)}).length`;
const submit = (sel, values) => evaluate(`(() => { const f = document.querySelector(${JSON.stringify(sel)}); ${Object.entries(values).map(([k, v]) => `f.elements[${JSON.stringify(k)}].value = ${JSON.stringify(v)};`).join('')} f.requestSubmit(); })()`);

try {
  await connect();
  await send('Runtime.enable'); await send('Page.enable');
  ws.addEventListener('message', (m) => {
    const msg = JSON.parse(m.data);
    if (msg.method === 'Runtime.exceptionThrown') consoleErrors.push(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text);
    if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') consoleErrors.push(msg.params.args.map((a) => a.value || a.description).join(' '));
  });

  // ---- hidden entry: no link anywhere, triple-click on the brand
  await goto(`${BASE}/#/`);
  check('no visible link to the admin page', !(await evaluate(`!!document.querySelector('a[href="#/admin"]')`)));
  await evaluate(`document.querySelector('#credits .brand').dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 3 }))`); await sleep(800);
  check('triple-click on the brand opens #/admin', await evaluate('location.hash') === '#/admin');
  check('password asked first', await evaluate(`!!document.querySelector('[data-form="admin-login"]')`) && !(await evaluate(`!!document.querySelector('.adm, table')`)));
  await submit('[data-form="admin-login"]', { password: 'wrong' }); await sleep(1500);
  check('wrong password rejected', (await evaluate(text('[data-form="admin-login"]'))).includes('incorrecta'));
  await submit('[data-form="admin-login"]', { password: 'admin' }); await sleep(1200);
  check('right password shows the sessions table', await evaluate(count('tr.sess')) >= 1, await evaluate(count('tr.sess')));

  // ---- sessions: name, open, undo one change, undo the whole session
  const sid = await evaluate(`document.querySelector('tr.sess td').textContent`);
  await submit(`[data-form="session-name"][data-id="${sid}"]`, { name: 'Prueba navegador' }); await sleep(1000);
  const named = (await (await fetch(`${BASE}/api/admin/sessions`, { headers: { Cookie: '' } })).status) === 401;
  check('sessions API stays private for other clients', named);
  check('session renamed inline', await evaluate(`document.querySelector('[data-form="session-name"][data-id="${sid}"] input').value`) === 'Prueba navegador');
  await evaluate(`document.querySelector('[data-action="adm-open-session"][data-id="${sid}"]').click()`); await sleep(1000);
  const items = await evaluate(count('ul.changes li'));
  check('opening a session lists its changes', items >= 3, items);
  check('changes link to the ficha and show old → new', await evaluate(`!!document.querySelector('ul.changes li a[href^="#/viaje/"]')`)
    && /pendiente → curado/.test(await evaluate(text('ul.changes'))));
  const undoId = await evaluate(`(() => { const li = [...document.querySelectorAll('ul.changes li')].find((l) => /pendiente → curado/.test(l.textContent)); return li ? li.querySelector('[data-action="adm-revert-change"]').dataset.id : null; })()`);
  check('the status change offers undo', !!undoId);
  const sessBefore = await evaluate(count('tr.sess'));
  await evaluate(`document.querySelector('[data-action="adm-revert-change"][data-id="${undoId}"]').click()`); await sleep(1200);
  check('undo reports and strikes the change through', (await evaluate(text('.msg'))).includes('revertido') && await evaluate(`document.getElementById('ch${undoId}').classList.contains('undone')`));
  // the undo is journaled under the admin's own browser session (a new row in the table)
  check('a new session row appears for the admin browser', await evaluate(count('tr.sess')) === sessBefore + 1);
  const adminSid = await evaluate(`document.querySelector('tr.sess td').textContent`);
  await evaluate(`document.querySelector('[data-action="adm-open-session"][data-id="${adminSid}"]').click()`); await sleep(1000);
  check('the undo itself is journaled there', (await evaluate(text('ul.changes'))).includes('deshace #' + undoId));

  await evaluate(`window.confirm = () => true`);
  await evaluate(`document.querySelector('[data-action="adm-revert-session"][data-id="${sid}"]').click()`); await sleep(2000);
  check('undo-all reports the count', /cambios deshechos/.test(await evaluate(text('.msg'))), await evaluate(text('.msg')));
  check('nothing left to undo in that session', !(await evaluate(`!!document.querySelector('[data-action="adm-revert-session"][data-id="${sid}"]')`)));

  // ---- landmark history
  await submit('[data-form="admin-search"]', { q: 'Casa da' }); await sleep(1200);
  check('history search lists the changes of a landmark', /Casa da M/.test(await evaluate(text('#history'))) && /sesión \d+/.test(await evaluate(text('#history'))));

  // ---- backups
  const before = await evaluate(count('[data-action="adm-restore"]'));
  await evaluate(`document.querySelector('[data-action="adm-backup-now"]').click()`); await sleep(2500);
  check('backup now adds a restorable copy', await evaluate(count('[data-action="adm-restore"]')) === before + 1 && /Copia creada: manual-/.test(await evaluate(text('.msg'))));
  check('SQL dump link present', await evaluate(`!!document.querySelector('a[href="/api/admin/export.db"]')`));

  // ---- logout
  await evaluate(`document.querySelector('[data-action="adm-logout"]').click()`); await sleep(1000);
  check('logout returns to the password form', await evaluate(`!!document.querySelector('[data-form="admin-login"]')`));
  check('no console errors', consoleErrors.length === 0, JSON.stringify(consoleErrors).slice(0, 600));
} catch (e) {
  console.log('ERROR', e.message); failures++;
} finally {
  try { ws?.close(); } catch {}
  chrome.kill();
  console.log(failures ? `\n${failures} FAILED` : '\nALL OK');
  process.exit(failures ? 1 : 0);
}
