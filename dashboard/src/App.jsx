import { useEffect, useState } from "react";
import { api } from "./api";
import StatusBar from "./components/StatusBar";
import OverviewPanel from "./components/OverviewPanel";
import TradesPanel from "./components/TradesPanel";
import NewsPanel from "./components/NewsPanel";
import ChartPanel from "./components/ChartPanel";
import WatcherPanel from "./components/WatcherPanel";
import "./App.css";

const TABS = ["Overview", "Chart", "News", "Trades", "Watcher"];

function App() {
  const [tab, setTab] = useState("Overview");
  const [watchlist, setWatchlist] = useState([]);
  const [ticker, setTicker] = useState(null);

  useEffect(() => {
    api
      .watchlist()
      .then((d) => {
        setWatchlist(d.watchlist);
        setTicker(d.watchlist[0] ?? null);
      })
      .catch(() => {});
  }, []);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">Trading Bot</div>
        <nav>
          {TABS.map((t) => (
            <button key={t} className={t === tab ? "nav-item active" : "nav-item"} onClick={() => setTab(t)}>
              {t}
            </button>
          ))}
        </nav>
      </aside>

      <div className="main">
        <header className="top-bar">
          <StatusBar />
          {(tab === "Chart" || tab === "News") && (
            <select className="ticker-select" value={ticker ?? ""} onChange={(e) => setTicker(e.target.value)}>
              {watchlist.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          )}
        </header>

        <main className="content">
          {tab === "Overview" && <OverviewPanel />}
          {tab === "Chart" && ticker && <ChartPanel ticker={ticker} />}
          {tab === "News" && <NewsPanel ticker={ticker} />}
          {tab === "Trades" && <TradesPanel />}
          {tab === "Watcher" && <WatcherPanel />}
        </main>
      </div>
    </div>
  );
}

export default App;
