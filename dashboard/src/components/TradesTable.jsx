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

// Shared by TradesPanel (full history) and OverviewPanel (recent-activity
// teaser) so the row markup and formatting only live in one place.
export default function TradesTable({ trades, emptyMessage = "No trades yet." }) {
  if (trades === null) return <div className="panel-loading">Loading trades…</div>;
  if (trades.length === 0) return <div className="panel-empty">{emptyMessage}</div>;

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
            <td className={t.action === "buy" ? "text-good" : "text-critical"}>{t.action.toUpperCase()}</td>
            <td>{t.quantity}</td>
            <td>{fmtMoney(t.price)}</td>
            <td className="text-muted">{t.reason}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
