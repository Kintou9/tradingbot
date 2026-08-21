const { app, BrowserWindow, Tray, Menu, nativeImage, ipcMain } = require("electron");
const path = require("path");
const http = require("http");
const { spawn } = require("child_process");

const PROJECT_ROOT = path.join(__dirname, "..", "..");
require("dotenv").config({ path: path.join(PROJECT_ROOT, ".env") });

const BACKEND_HEALTH_URL = "http://127.0.0.1:8000/health";
const DEV_SERVER_URL = process.env.ELECTRON_START_URL;

// Handed to the renderer over IPC (see preload.cjs) rather than baked
// into the Vite build — a built JS bundle is easy to read straight off
// disk, an IPC round-trip at least keeps the token out of that artifact.
ipcMain.handle("get-api-token", () => process.env.DASHBOARD_API_TOKEN ?? null);

let mainWindow = null;
let tray = null;
let backendProcess = null;

function checkBackendHealth() {
  return new Promise((resolve) => {
    const req = http.get(BACKEND_HEALTH_URL, (res) => {
      resolve(res.statusCode === 200);
      res.resume();
    });
    req.on("error", () => resolve(false));
    req.setTimeout(1000, () => {
      req.destroy();
      resolve(false);
    });
  });
}

// Spawns the FastAPI backend from the project's venv so this app is
// self-contained — no need to manually run uvicorn first. Skipped if
// something's already serving on :8000 (e.g. you started it yourself).
async function ensureBackendRunning() {
  const alreadyUp = await checkBackendHealth();
  if (alreadyUp) {
    console.log("Backend already running, not spawning a second one.");
    return;
  }

  const pythonBin = path.join(PROJECT_ROOT, "venv", "bin", "python3");
  backendProcess = spawn(pythonBin, ["-m", "uvicorn", "api.main:app", "--port", "8000"], {
    cwd: PROJECT_ROOT,
    stdio: "inherit",
  });

  backendProcess.on("exit", (code) => {
    console.log(`Backend process exited with code ${code}`);
    backendProcess = null;
  });

  // Wait for it to actually come up (up to ~15s) before loading the window.
  for (let i = 0; i < 30; i++) {
    if (await checkBackendHealth()) return;
    await new Promise((r) => setTimeout(r, 500));
  }
  console.warn("Backend did not become healthy within 15s — loading dashboard anyway.");
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    title: "Trading Bot",
    icon: path.join(__dirname, "assets", "icon.png"),
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  if (DEV_SERVER_URL) {
    mainWindow.loadURL(DEV_SERVER_URL);
  } else {
    mainWindow.loadFile(path.join(PROJECT_ROOT, "dashboard", "dist", "index.html"));
  }

  // Closing the window hides it instead of quitting — the app stays
  // running in the tray so the backend keeps working in the background.
  mainWindow.on("close", (event) => {
    if (!app.isQuitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
}

function createTray() {
  const icon = nativeImage.createFromPath(path.join(__dirname, "assets", "iconTemplate.png"));
  icon.setTemplateImage(true);
  tray = new Tray(icon);
  tray.setToolTip("Trading Bot");

  const menu = Menu.buildFromTemplate([
    { label: "Show Dashboard", click: () => mainWindow?.show() },
    { type: "separator" },
    {
      label: "Quit",
      click: () => {
        app.isQuitting = true;
        app.quit();
      },
    },
  ]);
  tray.setContextMenu(menu);

  tray.on("click", () => {
    if (mainWindow.isVisible()) {
      mainWindow.hide();
    } else {
      mainWindow.show();
    }
  });
}

app.whenReady().then(async () => {
  createTray();
  await ensureBackendRunning();
  createWindow();
});

app.on("window-all-closed", () => {
  // Intentionally not quitting here — this is a tray app, it should keep
  // running (and keep the backend alive) even with no window open.
});

app.on("before-quit", () => {
  app.isQuitting = true;
  if (backendProcess) {
    backendProcess.kill();
  }
});
