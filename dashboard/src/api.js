const BASE_URL = "http://127.0.0.1:8000";

let tokenPromise = null;

// Fetched once via the Electron preload bridge (see electron/preload.cjs)
// and cached — the token doesn't change while the app is running.
function getToken() {
  if (!tokenPromise) {
    tokenPromise = window.electronAPI ? window.electronAPI.getApiToken() : Promise.resolve(null);
  }
  return tokenPromise;
}

async function authHeaders() {
  const token = await getToken();
  return token ? { "X-API-Token": token } : {};
}

async function get(path) {
  const res = await fetch(`${BASE_URL}${path}`, { headers: await authHeaders() });
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`);
  return res.json();
}

async function post(path, body) {
  const headers = await authHeaders();
  const options = { method: "POST", headers };
  if (body !== undefined) {
    options.headers = { ...headers, "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const res = await fetch(`${BASE_URL}${path}`, options);
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail || `${path} failed: ${res.status}`);
  }
  return res.json();
}

async function del(path) {
  const res = await fetch(`${BASE_URL}${path}`, { method: "DELETE", headers: await authHeaders() });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail || `${path} failed: ${res.status}`);
  }
  return res.json();
}

export const api = {
  health: () => get("/health"),
  status: () => get("/status"),
  killSwitch: () => post("/kill-switch"),
  resume: () => post("/resume"),
  account: () => get("/account"),
  positions: () => get("/positions"),
  trades: () => get("/trades"),
  tradesSummary: () => get("/trades/summary"),
  watchlist: () => get("/watchlist"),
  watched: () => get("/watched"),
  addWatched: (ticker, notes) => post("/watched", { ticker, notes: notes || null }),
  removeWatched: (ticker) => del(`/watched/${ticker}`),
  news: (ticker) => get(`/news${ticker ? `?ticker=${ticker}` : ""}`),
  research: (ticker) => get(`/research${ticker ? `?ticker=${ticker}` : ""}`),
  chart: (ticker, outputsize = 100) => get(`/chart/${ticker}?outputsize=${outputsize}`),
};
