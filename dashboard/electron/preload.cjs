// Intentionally minimal — the renderer talks to the FastAPI backend
// directly over HTTP (see src/api.js), so no IPC bridge is needed yet.
// Kept as a file (rather than omitted) so contextIsolation has a defined,
// empty attack surface instead of the Electron default preload.
