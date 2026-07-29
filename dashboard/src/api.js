const BASE_URL = "http://127.0.0.1:8000";

async function get(path) {
  const res = await fetch(`${BASE_URL}${path}`);
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`);
  return res.json();
}

async function post(path) {
  const res = await fetch(`${BASE_URL}${path}`, { method: "POST" });
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`);
  return res.json();
}

export const api = {
  health: () => get("/health"),
  status: () => get("/status"),
  killSwitch: () => post("/kill-switch"),
  resume: () => post("/resume"),
  positions: () => get("/positions"),
  trades: () => get("/trades"),
  watchlist: () => get("/watchlist"),
  news: (ticker) => get(`/news${ticker ? `?ticker=${ticker}` : ""}`),
  research: (ticker) => get(`/research${ticker ? `?ticker=${ticker}` : ""}`),
  chart: (ticker, outputsize = 100) => get(`/chart/${ticker}?outputsize=${outputsize}`),
};
