/**
 * Alfred — Electron main process
 *
 * Responsibilities:
 *  1. Find a free local port
 *  2. Start the Python (FastAPI) backend as a hidden subprocess
 *  3. Start the WhatsApp Node bridge as a hidden subprocess
 *  4. Show a loading screen while the backend warms up
 *  5. Load the dashboard once the backend is ready
 *  6. Clean up all child processes on exit
 */

const { app, BrowserWindow, shell, Menu, Tray, nativeImage } = require('electron');
const { spawn }  = require('child_process');
const path       = require('path');
const http       = require('http');
const net        = require('net');
const fs         = require('fs');
const os         = require('os');

// ─── Constants ───────────────────────────────────────────────────────────────

const IS_DEV     = !app.isPackaged;
const IS_MAC     = process.platform === 'darwin';
const IS_WIN     = process.platform === 'win32';
const APP_ROOT   = IS_DEV
  ? path.join(__dirname, '..')                               // repo root in dev
  : path.join(process.resourcesPath, 'backend');             // bundled in prod

// User data dir — persists across app updates (~/Library/Application Support/Alfred etc.)
const USER_DATA  = app.getPath('userData');
const LOG_DIR    = path.join(USER_DATA, 'logs');
const ENV_FILE   = path.join(USER_DATA, '.env');             // user's .env lives here
const USERS_DIR  = path.join(USER_DATA, 'users');            // Alfred's data directory
const WA_SESSIONS = path.join(USER_DATA, 'wa-sessions');     // WhatsApp auth files

fs.mkdirSync(LOG_DIR,     { recursive: true });
fs.mkdirSync(USERS_DIR,   { recursive: true });
fs.mkdirSync(WA_SESSIONS, { recursive: true });

// If the user doesn't have a .env yet, copy the template from the app bundle
const ENV_TEMPLATE = IS_DEV
  ? path.join(APP_ROOT, '.env')
  : path.join(process.resourcesPath, '.env.template');
if (!fs.existsSync(ENV_FILE) && fs.existsSync(ENV_TEMPLATE)) {
  fs.copyFileSync(ENV_TEMPLATE, ENV_FILE);
}

// ─── State ────────────────────────────────────────────────────────────────────

let mainWindow    = null;
let tray          = null;
let pythonProc    = null;
let waProc        = null;
let backendPort   = null;

// ─── Helpers ─────────────────────────────────────────────────────────────────

function getFreePort() {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.listen(0, '127.0.0.1', () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
  });
}

function makeLogStream(name) {
  const file = path.join(LOG_DIR, `${name}.log`);
  return fs.createWriteStream(file, { flags: 'a' });
}

function waitForBackend(port, maxMs = 45000) {
  return new Promise((resolve) => {
    const deadline = Date.now() + maxMs;
    function attempt() {
      if (Date.now() > deadline) { resolve(false); return; }
      const req = http.get(`http://127.0.0.1:${port}/health`, (res) => {
        resolve(res.statusCode < 500);
      });
      req.on('error', () => setTimeout(attempt, 600));
      req.setTimeout(1000, () => { req.destroy(); setTimeout(attempt, 600); });
    }
    attempt();
  });
}

// ─── Start Python backend ─────────────────────────────────────────────────────

async function startPython(port) {
  const logStream = makeLogStream('alfred-python');
  const env = {
    ...process.env,
    PORT:               String(port),
    ALFRED_DATA_DIR:    USERS_DIR,
    ALFRED_ENV_FILE:    ENV_FILE,
    // Tell Alfred to write user data next to the .env, not inside the app bundle
    ALFRED_USERS_ROOT:  USERS_DIR,
  };

  if (IS_DEV) {
    // Dev: run uvicorn directly
    const python = IS_WIN ? 'python' : 'python3';
    const venvPython = IS_WIN
      ? path.join(APP_ROOT, 'venv', 'Scripts', 'python.exe')
      : path.join(APP_ROOT, 'venv', 'bin', 'python3');
    const bin = fs.existsSync(venvPython) ? venvPython : python;
    pythonProc = spawn(
      bin,
      ['-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', String(port)],
      { cwd: APP_ROOT, env, stdio: ['ignore', 'pipe', 'pipe'] }
    );
  } else {
    // Prod: run the PyInstaller bundle
    const exe = IS_WIN
      ? path.join(APP_ROOT, 'alfred-backend.exe')
      : path.join(APP_ROOT, 'alfred-backend');
    pythonProc = spawn(exe, ['--port', String(port)], { env, stdio: ['ignore', 'pipe', 'pipe'] });
  }

  pythonProc.stdout?.pipe(logStream);
  pythonProc.stderr?.pipe(logStream);

  pythonProc.on('exit', (code) => {
    logStream.write(`\n[Alfred] Python exited with code ${code}\n`);
  });
}

// ─── Start WhatsApp Node bridge ───────────────────────────────────────────────

function startWhatsAppBridge(alfredPort) {
  const bridgeScript = IS_DEV
    ? path.join(APP_ROOT, 'whatsapp', 'index.js')
    : path.join(process.resourcesPath, 'whatsapp', 'index.js');

  if (!fs.existsSync(bridgeScript)) return;

  const logStream = makeLogStream('alfred-whatsapp');
  const env = {
    ...process.env,
    ALFRED_API_URL:      `http://127.0.0.1:${alfredPort}`,
    WHATSAPP_BRIDGE_PORT: '3000',
    WA_SESSION_PATH:      WA_SESSIONS,   // persists across app restarts ✓
  };

  const node = IS_WIN ? 'node.exe' : 'node';
  waProc = spawn(node, [bridgeScript], {
    cwd: path.dirname(bridgeScript),
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  waProc.stdout?.pipe(logStream);
  waProc.stderr?.pipe(logStream);

  // Auto-restart if it crashes (same as entrypoint.sh)
  waProc.on('exit', (code) => {
    logStream.write(`\n[Alfred] WhatsApp bridge exited (${code}), restarting in 5s…\n`);
    if (!app.isQuitting) setTimeout(() => startWhatsAppBridge(alfredPort), 5000);
  });
}

// ─── Kill all child processes ─────────────────────────────────────────────────

function killAll() {
  try { pythonProc?.kill(); } catch (_) {}
  try { waProc?.kill();     } catch (_) {}
}

// ─── Create the app window ────────────────────────────────────────────────────

async function createWindow() {
  backendPort = await getFreePort();

  mainWindow = new BrowserWindow({
    width:          1300,
    height:         860,
    minWidth:       900,
    minHeight:      600,
    titleBarStyle:  IS_MAC ? 'hiddenInset' : 'default',
    backgroundColor: '#0a0404',
    webPreferences: {
      nodeIntegration:  false,
      contextIsolation: true,
    },
    show:  false,
    title: 'Alfred',
    icon:  path.join(__dirname, 'icons', IS_WIN ? 'icon.ico' : 'icon.png'),
  });

  // Remove default menu bar (looks cleaner)
  Menu.setApplicationMenu(null);

  // Show loading screen immediately
  mainWindow.loadFile(path.join(__dirname, 'loading.html'));
  mainWindow.once('ready-to-show', () => mainWindow.show());

  // Open external links in the real browser, not in Alfred
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // Boot backend + bridge
  await startPython(backendPort);
  startWhatsAppBridge(backendPort);

  // Wait until Python is ready, then load the dashboard
  const ready = await waitForBackend(backendPort);
  if (ready) {
    mainWindow.loadURL(`http://127.0.0.1:${backendPort}`);
  } else {
    mainWindow.loadFile(path.join(__dirname, 'error.html'));
  }

  mainWindow.on('closed', () => { mainWindow = null; });
}

// ─── System tray (optional — lets users access Alfred from the menu bar) ──────

function createTray() {
  const iconPath = path.join(__dirname, 'icons', IS_MAC ? 'trayTemplate.png' : 'icon.png');
  if (!fs.existsSync(iconPath)) return;

  tray = new Tray(nativeImage.createFromPath(iconPath));
  tray.setToolTip('Alfred');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Open Alfred', click: () => { mainWindow?.show(); mainWindow?.focus(); } },
    { type: 'separator' },
    { label: 'Quit',        click: () => { app.isQuitting = true; app.quit(); } },
  ]));
  tray.on('double-click', () => { mainWindow?.show(); mainWindow?.focus(); });
}

// ─── App lifecycle ────────────────────────────────────────────────────────────

app.whenReady().then(async () => {
  await createWindow();
  createTray();

  app.on('activate', () => {
    // macOS: re-open window when clicking dock icon
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
    else mainWindow?.show();
  });
});

app.on('window-all-closed', () => {
  // On macOS apps conventionally stay running until Cmd+Q
  if (!IS_MAC) { killAll(); app.quit(); }
});

app.on('before-quit', () => {
  app.isQuitting = true;
  killAll();
});
