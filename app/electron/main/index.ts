/**
 * Electron main entry (plan §3 Electron Main).
 *
 * Owns the BrowserWindow, registers IPC, and serves run-folder preview images to
 * the renderer over a custom `uvpreview://` protocol (sandboxed file access).
 */

import { app, BrowserWindow, protocol, net, nativeImage } from 'electron';
import { join } from 'path';
import { existsSync } from 'fs';
import { pathToFileURL } from 'url';
import { registerIpc } from './ipc';

// Set the product name early so the macOS dock tooltip + app menu read "Reforge"
// instead of the dev-mode "Electron" binary name (must run before app `ready`).
app.setName('Reforge');

// `<img src="uvpreview://…">` works unprivileged, but the 3D viewer `fetch()`es
// GLBs over the same scheme, and the fetch API + cross-origin use from the dev
// server need explicit privileges. Must run before app `ready`. `standard` stays
// false so the URL keeps its raw absolute-path form (no host parsing/lowercasing).
protocol.registerSchemesAsPrivileged([
  {
    scheme: 'uvpreview',
    privileges: { secure: true, supportFetchAPI: true, corsEnabled: true, bypassCSP: true, stream: true },
  },
]);

const isDev = !!process.env['ELECTRON_RENDERER_URL'];

/** Resolve the Reforge app icon (app/resources/icon.png in dev, bundled resources
 *  when packaged). Returns null if missing so window creation never fails on it. */
function loadAppIcon(): Electron.NativeImage | null {
  const candidates = app.isPackaged
    ? [join(process.resourcesPath, 'icon.png')]
    : [join(__dirname, '../../resources/icon.png')];
  for (const p of candidates) {
    if (existsSync(p)) {
      const img = nativeImage.createFromPath(p);
      if (!img.isEmpty()) return img;
    }
  }
  return null;
}

function createWindow(): void {
  const icon = loadAppIcon();
  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 640,
    title: 'Reforge',
    ...(icon ? { icon } : {}),
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  if (isDev) {
    win.loadURL(process.env['ELECTRON_RENDERER_URL'] as string);
  } else {
    win.loadFile(join(__dirname, '../renderer/index.html'));
  }
}

app.whenReady().then(() => {
  // Serve local preview PNGs / GLBs the renderer references by absolute path.
  protocol.handle('uvpreview', async (request) => {
    let filePath = decodeURIComponent(request.url.replace('uvpreview://', ''));
    // The renderer prefixes Windows paths with '/' so the URL authority stays
    // empty ('uvpreview:///C:/…'); strip it back off before hitting the fs.
    if (/^\/[A-Za-z]:[\\/]/.test(filePath)) filePath = filePath.slice(1);
    const res = await net.fetch(pathToFileURL(filePath).toString());
    // Re-wrap so we can attach CORS (dev server origin fetches this scheme) and
    // a correct MIME for GLB (net.fetch guesses none for .glb).
    const headers = new Headers(res.headers);
    headers.set('Access-Control-Allow-Origin', '*');
    if (filePath.toLowerCase().endsWith('.glb')) headers.set('Content-Type', 'model/gltf-binary');
    return new Response(res.body, { status: res.status, headers });
  });

  // macOS shows the dock icon (BrowserWindow `icon` is ignored there).
  if (process.platform === 'darwin' && app.dock) {
    const icon = loadAppIcon();
    if (icon) app.dock.setIcon(icon);
  }

  registerIpc();
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
