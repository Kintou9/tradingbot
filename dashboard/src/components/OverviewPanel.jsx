import { useEffect, useState } from "react";
import { api } from "../api";
import PositionsPanel from "./PositionsPanel";
import TradesTable from "./TradesTable";

function fmtMoney(n) {
  return n.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}

function fmtPct(n) {
  return `${(n * 100).toFixed(1)}%`;
}

export default function OverviewPanel() {
  const [account, setAccount] = useState(null);
  const [accountError, setAccountError] = useState(null);
  const [summary, setSummary] = useState(null);
  const [recentTrades, setRecentTrades] = useState(null);

  useEffect(() => {
    const refresh = () => {
      api
        .account()
        .then((d) => {
          setAccount(d);
          setAccountError(null);
        })
        .catch((e) => setAccountError(e.message));
    };
    refresh();
    const id = setInterval(refresh, 20000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    api.tradesSummary().then(setSummary).catch(() => {});
    api
      .trades()
      .then((d) => setRecentTrades(d.trades.slice(0, 5)))
      .catch(() => setRecentTrades([]));
  }, []);

  return (
    <div className="overview">
      <div className="research-cards">
        <div className="research-card">
          <h4>Account</h4>
          {accountError ? (
            <div className="panel-error">{accountError}</div>
          ) : account ? (
            <dl>
              <dt>Equity</dt>
              <dd>{fmtMoney(account.equity)}</dd>
              <dt>Today&rsquo;s P&amp;L</dt>
              <dd className={account.daily_pl >= 0 ? "text-good" : "text-critical"}>
                {account.daily_pl >= 0 ? "+" : ""}
                {fmtMoney(account.daily_pl)} ({fmtPct(account.daily_pl_pct)})
              </dd>
            </dl>
          ) : (
            <div className="panel-loading">Loading…</div>
          )}
        </div>

        <div className="research-card">
          <h4>Daily Loss Limit</h4>
          {account ? (
            <dl>
              <dt>Limit</dt>
              <dd>{fmtPct(account.daily_loss_limit_pct)}</dd>
              <dt>Headroom Left</dt>
              <dd className={account.daily_loss_headroom_pct <= 0 ? "text-critical" : undefined}>
                {fmtPct(account.daily_loss_headroom_pct)}
              </dd>
            </dl>
          ) : (
            <div className="panel-loading">Loading…</div>
          )}
        </div>

        <div className="research-card">
          <h4>Trade History</h4>
          {summary ? (
            <dl>
              <dt>Realized P&amp;L</dt>
              <dd className={summary.realized_pl >= 0 ? "text-good" : "text-critical"}>
                {summary.realized_pl >= 0 ? "+" : ""}
                {fmtMoney(summary.realized_pl)}
              </dd>
              <dt>Win Rate</dt>
              <dd>{summary.win_rate_pct != null ? `${summary.win_rate_pct.toFixed(0)}%` : "—"}</dd>
              <dt>Total Trades</dt>
              <dd>{summary.total_trades}</dd>
            </dl>
          ) : (
            <div className="panel-loading">Loading…</div>
          )}
        </div>
      </div>

      <section className="overview-section">
        <h3 className="section-heading">Positions</h3>
        <PositionsPanel />
      </section>

      <section className="overview-section">
        <h3 className="section-heading">Recent Activity</h3>
        <TradesTable trades={recentTrades} />
      </section>
    </div>
  );
}
