import { useEffect, useState } from "react";
import { api } from "../api";

function fmtMoney(n) {
  return n.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}

export default function PositionsPanel() {
  const [positions, setPositions] = useState(null);
  const [error, setError] = useState(null);

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

  if (error) return <div className="panel-error">Failed to load positions: {error}</div>;
  if (positions === null) return <div className="panel-loading">Loading positions…</div>;
  if (positions.length === 0) return <div className="panel-empty">No open positions.</div>;

  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>Ticker</th>
          <th>Qty</th>
          <th>Avg Entry</th>
          <th>Current Price</th>
          <th>Unrealized P&amp;L</th>
        </tr>
      </thead>
      <tbody>
        {positions.map((p) => {
          const positive = p.unrealized_pl >= 0;
          return (
            <tr key={p.ticker}>
              <td className="ticker-cell">{p.ticker}</td>
              <td>{p.qty}</td>
              <td>{fmtMoney(p.avg_entry_price)}</td>
              <td>{fmtMoney(p.current_price)}</td>
              <td className={positive ? "text-good" : "text-critical"}>
                {positive ? "+" : ""}
                {fmtMoney(p.unrealized_pl)}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
