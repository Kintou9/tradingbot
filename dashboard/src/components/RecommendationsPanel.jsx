import { useEffect, useState } from "react";
import { api } from "../api";

function fmtDate(iso) {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// Candidates not currently held, scored against the same gates the bot's
// own evaluate_entry uses — from whatever research is already cached.
// Doesn't trigger new research itself; a ticker with no DCF yet shows up
// as "valuation unknown" rather than getting silently skipped.
export default function RecommendationsPanel() {
  const [recs, setRecs] = useState(null);
  const [error, setError] = useState(null);
  const [buying, setBuying] = useState(null);
  const [actionError, setActionError] = useState(null);

  const refresh = () => {
    api
      .recommendations()
      .then((d) => {
        setRecs(d.recommendations);
        setError(null);
      })
      .catch((e) => setError(e.message));
  };

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 60000);
    return () => clearInterval(id);
  }, []);

  const handleBuy = async (r) => {
    if (!window.confirm(`Submit a protected buy for ${r.ticker} within the server’s trade and exposure limits?`)) return;
    setBuying(r.ticker);
    setActionError(null);
    try {
      await api.buy(r.ticker);
      refresh();
    } catch (err) {
      setActionError(`${r.ticker}: ${err.message}`);
    } finally {
      setBuying(null);
    }
  };

  if (error) return <div className="panel-error">Failed to load recommendations: {error}</div>;
  if (recs === null) return <div className="panel-loading">Loading candidates…</div>;
  if (recs.length === 0) return <div className="panel-empty">No candidates on the watchlist or Watcher list.</div>;

  const clearing = recs.filter((r) => r.clears_entry_gate);
  const rest = recs.filter((r) => !r.clears_entry_gate);

  return (
    <>
      {actionError && <div className="panel-error">{actionError}</div>}
      {clearing.length === 0 && (
        <div className="panel-empty">
          Nothing currently clears the 20%-discount / uptrend / sentiment gate — see reasons below.
        </div>
      )}
      <table className="data-table">
        <thead>
          <tr>
            <th>Ticker</th>
            <th>Company</th>
            <th>Source</th>
            <th>Status</th>
            <th>Researched</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {[...clearing, ...rest].map((r) => (
            <tr key={r.ticker}>
              <td className="ticker-cell">{r.ticker}</td>
              <td>{r.company_name || <span className="text-muted">—</span>}</td>
              <td className="text-muted">
                {r.on_watchlist && r.on_watcher ? "Watchlist + Watcher" : r.on_watchlist ? "Watchlist" : "Watcher"}
              </td>
              <td>
                {r.clears_entry_gate ? (
                  <span className="verdict-pill verdict-buy">{r.reason}</span>
                ) : (
                  <span className="text-muted">{r.reason}</span>
                )}
              </td>
              <td className="text-muted">{r.researched_at ? fmtDate(r.researched_at) : "—"}</td>
              <td>
                {r.clears_entry_gate && (
                  <button
                    className="watcher-add-button"
                    disabled={buying === r.ticker}
                    onClick={() => handleBuy(r)}
                  >
                    {buying === r.ticker ? "Buying…" : "Buy"}
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
