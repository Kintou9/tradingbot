import { useEffect, useState } from "react";
import { api } from "../api";

function fmtMoney(n) {
  return n.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}

function fmtTime(iso) {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function TradesPanel() {
  const [trades, setTrades] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api
      .trades()
      .then((d) => setTrades(d.trades))
      .catch((e) => setError(e.message));
  }, []);

  if (error) return <div className="panel-error">Failed to load trades: {error}</div>;
  if (trades === null) return <div className="panel-loading">Loading trades…</div>;
  if (trades.length === 0) return <div className="panel-empty">No trades yet.</div>;

  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>Time</th>
          <th>Ticker</th>
          <th>Action</th>
          <th>Qty</th>
          <th>Price</th>
          <th>Reason</th>
        </tr>
      </thead>
      <tbody>
        {trades.map((t, i) => (
          <tr key={i}>
            <td>{fmtTime(t.timestamp)}</td>
            <td className="ticker-cell">{t.ticker}</td>
            <td className={t.action === "buy" ? "text-good" : "text-critical"}>
              {t.action.toUpperCase()}
            </td>
            <td>{t.quantity}</td>
            <td>{fmtMoney(t.price)}</td>
            <td className="text-muted">{t.reason}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
