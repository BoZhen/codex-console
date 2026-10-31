// Optional desktop check; needs playwright-core, Electron and the Console Python environment.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { _electron: electron } = require('playwright-core');
const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'console-rename-electron-'));
const output = process.env.SCREENSHOT_DIR || path.join(os.tmpdir(), 'console-rename-electron');
const html = execFileSync(process.env.CONSOLE_PYTHON || 'python3', ['-c',
  'import codex_console as c; print(c.CONSOLE_HTML.replace("__CODEX_CONSOLE_WEBFM_URL__", "null").replace("__CODEX_CONSOLE_STAMP__", c.PAGE_STAMP))'
], { cwd: root, encoding: 'utf8' });
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture.test');
  if (url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    res.end(url.pathname === '/api/project-library' ? '{"sessions":[],"folders":[],"groups":[]}' : '{"projects":[],"models":[],"available":false}'); return;
  }
  if (url.pathname.startsWith('/static/')) {
    const file = path.resolve(root, '.' + url.pathname);
    if (!file.startsWith(path.join(root, 'static') + path.sep) || !fs.existsSync(file)) {
      res.writeHead(404); res.end(); return;
    }
    const types = { '.js': 'application/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.png': 'image/png' };
    res.setHeader('Content-Type', types[path.extname(file)] || 'application/octet-stream');
    res.end(fs.readFileSync(file)); return;
  }
  res.setHeader('Content-Type', 'text/html; charset=utf-8'); res.end(html);
});

(async () => {
  let app;
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const entry = path.join(temporary, 'main.cjs');
    fs.writeFileSync(entry, [
      "const {app,BrowserWindow}=require('electron');",
      "app.setPath('userData',process.env.RENAME_TEST_DATA);",
      "app.whenReady().then(()=>new BrowserWindow({width:1000,height:800,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false}}).loadURL('about:blank'));"
    ].join('\n'));
    app = await electron.launch({ executablePath: process.env.ELECTRON_EXECUTABLE || require('electron'),
      args: [entry], chromiumSandbox: true, env: { ...process.env, RENAME_TEST_DATA: path.join(temporary, 'profile') }, timeout: 30000 });
    const page = await app.firstWindow();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.dismiss());
    await page.addInitScript(() => {
      window.__sent = [];
      window.WebSocket = class {
        constructor() { this.readyState = 1; }
        send(raw) {
          const message = JSON.parse(raw); window.__sent.push(message);
          if (message.type === 'rename') setTimeout(() => window.onMsg({ data: JSON.stringify({
            type: 'renamed', cc: message.cc, requestId: message.requestId, ok: true,
            name: message.name, title: message.name || 'Automatic task'
          }) }), 30);
        }
      };
    });
    await page.goto('http://127.0.0.1:' + server.address().port + '/');
    const nativePrompt = await page.evaluate(() => {
      try { window.prompt('Synthetic prompt probe', ''); return 'available'; }
      catch (error) { return error.message; }
    });
    console.log('Electron native prompt:', nativePrompt);
    await page.evaluate(() => {
      sid = 'live'; curCC = 'fixture-thread-one'; cwd = '/demo/project'; ready = true; running = true;
      const session = { id: sid, cc: curCC, cwd, title: 'Original task' };
      ensureSessionTab(session, true); setCurname(session.title);
      document.getElementById('liveList').replaceChildren(liveSessRow(session));
    });
    await page.locator('#liveList .skebab').click();
    await page.locator('#cardMenu .mi').filter({ hasText: 'Rename' }).click();
    await page.locator('#renameName').fill('Reviewed run');
    fs.mkdirSync(output, { recursive: true });
    await page.screenshot({ path: path.join(output, 'rename-electron.png') });
    await page.locator('#renameSave').click();
    await page.waitForFunction(() => !document.getElementById('renameDialog').open);
    assert.equal(await page.locator('#curname').textContent(), 'Reviewed run');
    assert.equal(await page.locator('#sessionTabs .stab-label').textContent(), 'Reviewed run');
    assert.equal(await page.evaluate(() => window.__sent.filter(m => m.type === 'rename').length), 1);
    assert.equal(await page.evaluate(() => window.__sent.some(m => ['start','resume','interrupt','end'].includes(m.type))), false);
    assert.deepEqual(errors, []);
    console.log('Electron rename menu, page dialog, save acknowledgement and title update PASS');
  } finally {
    if (app) await app.close();
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(temporary, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
