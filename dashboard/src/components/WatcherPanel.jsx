import { Fragment, useEffect, useState } from "react";
import { api } from "../api";

function fmtDate(iso) {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function fmtDateTime(iso) {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function WatcherDetail({ watched }) {
  const deepDive = watched.deep_dive?.structured;
  const technical = watched.technical?.structured;

  return (
    <div className="research-cards watcher-detail">
      <div className="research-card">
        <h4>Deep Dive</h4>
        {deepDive ? (
          <>
            <div className={`verdict-pill verdict-${deepDive.verdict}`}>{deepDive.verdict}</div>
            <dl>
              <dt>Confidence</dt>
              <dd>{deepDive.confidence != null ? `${deepDive.confidence}%` : "—"}</dd>
              <dt>Moat Score</dt>
              <dd>{deepDive.moat_score != null ? `${deepDive.moat_score}/100` : "—"}</dd>
            </dl>
            {deepDive.top_bullish_catalysts?.length > 0 && (
              <>
                <div className="watcher-detail-label">Bullish</div>
                <ul className="watcher-detail-list">
                  {deepDive.top_bullish_catalysts.map((c, i) => (
                    <li key={i}>{c}</li>
                  ))}
                </ul>
              </>
            )}
            {deepDive.top_bearish_risks?.length > 0 && (
              <>
                <div className="watcher-detail-label">Bearish</div>
                <ul className="watcher-detail-list">
                  {deepDive.top_bearish_risks.map((r, i) => (
                    <li key={i}>{r}</li>
                  ))}
                </ul>
              </>
            )}
            <div className="text-muted watcher-detail-date">Researched {fmtDateTime(watched.deep_dive.created_at)}</div>
          </>
        ) : (
          <div className="text-muted">No deep-dive research yet.</div>
        )}
      </div>

      <div className="research-card">
        <h4>Technical</h4>
        {technical ? (
          <>
            <div className={`verdict-pill trend-${technical.trend}`}>{technical.trend}</div>
            <dl>
              <dt>Entry</dt>
              <dd>{technical.entry_price ?? "—"}</dd>
              <dt>Stop</dt>
              <dd>{technical.stop_loss ?? "—"}</dd>
              <dt>Target 1</dt>
              <dd>{technical.target_1 ?? "—"}</dd>
              <dt>Target 2</dt>
              <dd>{technical.target_2 ?? "—"}</dd>
              <dt>Reward:Risk</dt>
              <dd>{technical.reward_risk_ratio ?? "—"}</dd>
              <dt>Probability</dt>
              <dd>{technical.probability_estimate ?? "—"}%</dd>
              <dt>Mean Reversion</dt>
              <dd>{technical.mean_reversion_bias ?? "—"}</dd>
            </dl>
            <div className="text-muted watcher-detail-date">Researched {fmtDateTime(watched.technical.created_at)}</div>
          </>
        ) : (
          <div className="text-muted">No technical scan yet.</div>
        )}
      </div>
    </div>
  );
}

function DiscoveryBanner({ onDiscovered }) {
  const [status, setStatus] = useState(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);

  const refresh = () => {
    api.discoveryStatus().then(setStatus).catch(() => {});
  };

  useEffect(refresh, []);

  const handleRunNow = async () => {
    if (
      !window.confirm(
        "Run stock discovery now? This adds up to 10 new tickers to the Watcher list " +
          "(one per sector/theme, plus real computed low/high-volatility and momentum picks). " +
          "It also counts as this week's automatic Friday run."
      )
    ) {
      return;
    }
    setRunning(true);
    setError(null);
    try {
      await api.runDiscoveryNow();
      refresh();
      onDiscovered();
    } catch (err) {
      setError(err.message);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="discovery-banner">
      <div>
        {status?.last_run_date ? (
          <>
            <span className="text-muted">Last auto-discovery {status.last_run_date}:</span>{" "}
            {status.last_run_tickers.join(", ")}
          </>
        ) : (
          <span className="text-muted">No discovery run yet — runs automatically every Friday after 4pm ET.</span>
        )}
        {error && <div className="panel-error">{error}</div>}
      </div>
      <button className="watcher-add-button" onClick={handleRunNow} disabled={running}>
        {running ? "Running… (~1 min)" : "Discover Now"}
      </button>
    </div>
  );
}

export default function WatcherPanel() {
  const [watched, setWatched] = useState(null);
  const [error, setError] = useState(null);
  const [ticker, setTicker] = useState("");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState(null);
  const [expanded, setExpanded] = useState(null);

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

  const toggleExpanded = (t) => {
    setExpanded((current) => (current === t ? null : t));
  };

  return (
    <div className="overview">
      <DiscoveryBanner onDiscovered={refresh} />
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
              <th>Company</th>
              <th>Notes</th>
              <th>Added</th>
              <th>Latest Verdict</th>
              <th>Latest Trend</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {watched.map((w) => {
              const isExpanded = expanded === w.ticker;
              return (
                <Fragment key={w.ticker}>
                  <tr
                    className="watcher-row"
                    onClick={() => toggleExpanded(w.ticker)}
                  >
                    <td className="ticker-cell">
                      <span className={`watcher-caret ${isExpanded ? "watcher-caret-open" : ""}`}>▸</span>
                      {w.ticker}
                    </td>
                    <td>{w.company_name || <span className="text-muted">—</span>}</td>
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
                      <button
                        className="watcher-remove-button"
                        onClick={(e) => {
                          e.stopPropagation();
                          handleRemove(w.ticker);
                        }}
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                  {isExpanded && (
                    <tr className="watcher-detail-row">
                      <td colSpan={7}>
                        <WatcherDetail watched={w} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
