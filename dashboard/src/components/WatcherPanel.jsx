import { useEffect, useState } from "react";
import { api } from "../api";

function fmtDate(iso) {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export default function WatcherPanel() {
  const [watched, setWatched] = useState(null);
  const [error, setError] = useState(null);
  const [ticker, setTicker] = useState("");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState(null);

  const refresh = () => {
    api
      .watched()
      .then((d) => {
        setWatched(d.watched);
        setError(null);
      })
      .catch((e) => setError(e.message));
  };

  useEffect(refresh, []);

  const handleAdd = async (e) => {
    e.preventDefault();
    if (!ticker.trim()) return;
    setSubmitting(true);
    setFormError(null);
    try {
      await api.addWatched(ticker.trim(), notes.trim());
      setTicker("");
      setNotes("");
      refresh();
    } catch (err) {
      setFormError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  const handleRemove = async (t) => {
    try {
      await api.removeWatched(t);
      refresh();
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <div className="overview">
      <form className="watcher-form" onSubmit={handleAdd}>
        <input
          className="watcher-input"
          placeholder="Ticker (e.g. NVDA)"
          value={ticker}
          onChange={(e) => setTicker(e.target.value)}
          maxLength={10}
        />
        <input
          className="watcher-input watcher-input-notes"
          placeholder="Why are you watching it? (optional)"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
        <button className="watcher-add-button" type="submit" disabled={submitting || !ticker.trim()}>
          Add
        </button>
      </form>
      {formError && <div className="panel-error">{formError}</div>}

      {error && <div className="panel-error">Failed to load watch list: {error}</div>}
      {!error && watched === null && <div className="panel-loading">Loading…</div>}
      {!error && watched !== null && watched.length === 0 && (
        <div className="panel-empty">No candidates yet — add a ticker above to start tracking it.</div>
      )}
      {!error && watched !== null && watched.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Ticker</th>
              <th>Notes</th>
              <th>Added</th>
              <th>Latest Verdict</th>
              <th>Latest Trend</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {watched.map((w) => (
              <tr key={w.ticker}>
                <td className="ticker-cell">{w.ticker}</td>
                <td className="text-muted">{w.notes || "—"}</td>
                <td className="text-muted">{fmtDate(w.added_at)}</td>
                <td>
                  {w.latest_verdict ? (
                    <span className={`verdict-pill verdict-${w.latest_verdict}`}>{w.latest_verdict}</span>
                  ) : (
                    <span className="text-muted">No research yet</span>
                  )}
                </td>
                <td>
                  {w.latest_trend ? (
                    <span className={`verdict-pill trend-${w.latest_trend}`}>{w.latest_trend}</span>
                  ) : (
                    <span className="text-muted">—</span>
                  )}
                </td>
                <td>
                  <button className="watcher-remove-button" onClick={() => handleRemove(w.ticker)}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
