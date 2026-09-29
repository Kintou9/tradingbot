import { Fragment, useEffect, useState } from "react";
import { api } from "../api";

function fmtMoney(n) {
  return n.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}

function fmtMoneyOrDash(n) {
  return n == null ? "—" : fmtMoney(n);
}

function fmtDateTime(iso) {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// Research goes stale the same way it does for the trading loop itself
// (see RESEARCH_MAX_AGE_HOURS in functions/market_hours_trading.py) —
// flag it here too so "why hasn't this changed" has an answer on screen.
function isStale(iso) {
  return Date.now() - new Date(iso).getTime() > 24 * 60 * 60 * 1000;
}

function latestByModule(notes, module) {
  return notes.find((n) => n.module === module) ?? null;
}

function HoldingDetail({ ticker }) {
  const [notes, setNotes] = useState(null);
  const [news, setNews] = useState(null);

  useEffect(() => {
    api
      .research(ticker)
      .then((d) => setNotes(d.research))
      .catch(() => setNotes([]));
    api
      .news(ticker)
      .then((d) => setNews(d.news.slice(0, 4)))
      .catch(() => setNews([]));
  }, [ticker]);

  if (notes === null) return <div className="panel-loading">Loading research…</div>;

  const deepDiveNote = latestByModule(notes, "deep_dive");
  const deepDive = deepDiveNote?.structured_output;
  const dcfNote = latestByModule(notes, "dcf");
  const dcf = dcfNote?.structured_output;
  const technicalNote = latestByModule(notes, "technical_scan");
  const technical = technicalNote?.structured_output;

  return (
    <div>
      <div className="research-cards holding-detail">
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
              <div className={`text-muted watcher-detail-date${isStale(deepDiveNote.created_at) ? " text-critical" : ""}`}>
                Researched {fmtDateTime(deepDiveNote.created_at)}
                {isStale(deepDiveNote.created_at) ? " (stale)" : ""}
              </div>
            </>
          ) : (
            <div className="text-muted">No deep-dive research yet.</div>
          )}
        </div>

        <div className="research-card">
          <h4>DCF Valuation</h4>
          {dcf ? (
            <>
              {dcf.share_count_source === "unavailable" && (
                <div className="verdict-pill verdict-avoid">Unreliable — no filed share count</div>
              )}
              <dl>
                <dt>Fair Value / Share</dt>
                <dd>{dcf.estimated_fair_value_per_share != null ? fmtMoney(dcf.estimated_fair_value_per_share) : "—"}</dd>
                <dt>WACC</dt>
                <dd>{dcf.wacc != null ? `${(dcf.wacc * 100).toFixed(1)}%` : "—"}</dd>
                <dt>Terminal Growth</dt>
                <dd>{dcf.terminal_growth_rate != null ? `${(dcf.terminal_growth_rate * 100).toFixed(1)}%` : "—"}</dd>
              </dl>
              {dcf.most_sensitive_assumptions?.length > 0 && (
                <>
                  <div className="watcher-detail-label">Most Sensitive To</div>
                  <ul className="watcher-detail-list">
                    {dcf.most_sensitive_assumptions.map((a, i) => (
                      <li key={i}>{a}</li>
                    ))}
                  </ul>
                </>
              )}
              <div className={`text-muted watcher-detail-date${isStale(dcfNote.created_at) ? " text-critical" : ""}`}>
                Researched {fmtDateTime(dcfNote.created_at)}
                {isStale(dcfNote.created_at) ? " (stale)" : ""}
              </div>
            </>
          ) : (
            <div className="text-muted">No DCF yet.</div>
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
              <div className={`text-muted watcher-detail-date${isStale(technicalNote.created_at) ? " text-critical" : ""}`}>
                Researched {fmtDateTime(technicalNote.created_at)}
                {isStale(technicalNote.created_at) ? " (stale)" : ""}
              </div>
            </>
          ) : (
            <div className="text-muted">No technical scan yet.</div>
          )}
        </div>
      </div>

      <div className="holding-news">
        <div className="watcher-detail-label">Recent News</div>
        {news === null ? (
          <div className="panel-loading">Loading news…</div>
        ) : news.length === 0 ? (
          <div className="text-muted">No recent news.</div>
        ) : (
          <ul className="watcher-detail-list holding-news-list">
            {news.map((n, i) => (
              <li key={i}>
                <span className={n.sentiment_score > 0 ? "text-good" : n.sentiment_score < 0 ? "text-critical" : "text-muted"}>
                  {n.sentiment_score != null ? n.sentiment_score.toFixed(2) : "—"}
                </span>{" "}
                {n.headline}
                <span className="text-muted"> — {n.source}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// Currently-held positions with an expandable row per ticker — the trade
// fill itself is just a price and a timestamp; this is where "is this
// still a good idea" actually lives (latest valuation/technical read,
// how stale it is, and what news is moving it).
export default function HoldingsPanel() {
  const [positions, setPositions] = useState(null);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const [selling, setSelling] = useState(null);
  const [actionError, setActionError] = useState(null);

  const refresh = () => {
    api
      .positions()
      .then((d) => {
        setPositions(d.positions);
        setError(null);
      })
      .catch((e) => setError(e.message));
  };

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 20000);
    return () => clearInterval(id);
  }, []);

  const toggleExpanded = (t) => setExpanded((current) => (current === t ? null : t));

  const handleSell = async (p) => {
    if (!window.confirm(`Request a sale of ${p.qty} shares of ${p.ticker}? Existing protective orders must be canceled first; completion may require a retry after cancellation is confirmed.`)) return;
    setSelling(p.ticker);
    setActionError(null);
    try {
      await api.sell(p.ticker);
      refresh();
    } catch (err) {
      setActionError(`${p.ticker}: ${err.message}`);
    } finally {
      setSelling(null);
    }
  };

  if (error) return <div className="panel-error">Failed to load holdings: {error}</div>;
  if (positions === null) return <div className="panel-loading">Loading holdings…</div>;
  if (positions.length === 0) return <div className="panel-empty">No open positions.</div>;

  return (
    <>
      {actionError && <div className="panel-error">{actionError}</div>}
      <table className="data-table">
        <thead>
          <tr>
            <th>Ticker</th>
            <th>Qty</th>
            <th>Avg Entry</th>
            <th>Current</th>
            <th>Stop</th>
            <th>Target</th>
            <th>Unrealized P&amp;L</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => {
            const isExpanded = expanded === p.ticker;
            const positive = p.unrealized_pl >= 0;
            return (
              <Fragment key={p.ticker}>
                <tr className="watcher-row" onClick={() => toggleExpanded(p.ticker)}>
                  <td className="ticker-cell">
                    <span className={`watcher-caret ${isExpanded ? "watcher-caret-open" : ""}`}>▸</span>
                    {p.ticker}
                  </td>
                  <td>{p.qty}</td>
                  <td>{fmtMoney(p.avg_entry_price)}</td>
                  <td>{fmtMoney(p.current_price)}</td>
                  <td className="text-muted">{fmtMoneyOrDash(p.stop_loss_price)}</td>
                  <td className="text-muted">{fmtMoneyOrDash(p.take_profit_price)}</td>
                  <td className={positive ? "text-good" : "text-critical"}>
                    {positive ? "+" : ""}
                    {fmtMoney(p.unrealized_pl)}
                  </td>
                  <td>
                    <button
                      className="watcher-remove-button"
                      disabled={selling === p.ticker}
                      onClick={(e) => {
                        e.stopPropagation();
                        handleSell(p);
                      }}
                    >
                      {selling === p.ticker ? "Selling…" : "Sell"}
                    </button>
                  </td>
                </tr>
                {isExpanded && (
                  <tr className="watcher-detail-row">
                    <td colSpan={8}>
                      <HoldingDetail ticker={p.ticker} />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </>
  );
}
