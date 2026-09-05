import { useEffect, useState } from "react";
import { api } from "../api";
import TradesTable from "./TradesTable";

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
  return <TradesTable trades={trades} />;
}
