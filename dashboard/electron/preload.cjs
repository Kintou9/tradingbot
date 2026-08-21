const { contextBridge, ipcRenderer } = require("electron");

// The only bridge exposed to the renderer: fetching the API token it
// needs to authenticate to the FastAPI backend. Everything else the
// renderer needs, it gets over plain HTTP (see src/api.js).
contextBridge.exposeInMainWorld("electronAPI", {
  getApiToken: () => ipcRenderer.invoke("get-api-token"),
});
