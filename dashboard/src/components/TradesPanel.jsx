import { useEffect, useState } from "react";
import { api } from "../api";
import TradesTable from "./TradesTable";
import HoldingsPanel from "./HoldingsPanel";

export default function TradesPanel() {
  const [trades, setTrades] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    const refresh = () => {
      api
        .trades()
        .then((d) => {
          setTrades(d.trades);
          setError(null);
        })
        .catch((e) => setError(e.message));
    };
    refresh();
    const id = setInterval(refresh, 20000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="overview">
      <section className="overview-section">
        <h3 className="section-heading">Holdings</h3>
        <HoldingsPanel />
      </section>

      <section className="overview-section">
        <h3 className="section-heading">Trade History</h3>
        {error ? (
          <div className="panel-error">Failed to load trades: {error}</div>
        ) : (
          <TradesTable trades={trades} />
        )}
      </section>
    </div>
  );
}
