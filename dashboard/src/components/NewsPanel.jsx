import { useEffect, useState } from "react";
import { api } from "../api";

function sentimentClass(score) {
  if (score === null || score === undefined) return "text-muted";
  if (score > 0.15) return "text-good";
  if (score < -0.15) return "text-critical";
  return "text-muted";
}

function fmtTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export default function NewsPanel({ ticker }) {
  const [news, setNews] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    setNews(null);
    api
      .news(ticker)
      .then((d) => setNews(d.news))
      .catch((e) => setError(e.message));
  }, [ticker]);

  if (error) return <div className="panel-error">Failed to load news: {error}</div>;
  if (news === null) return <div className="panel-loading">Loading news…</div>;
  if (news.length === 0) return <div className="panel-empty">No news items cached yet.</div>;

  return (
    <ul className="news-list">
      {news.map((n, i) => (
        <li key={i} className="news-item">
          <div className="news-item-top">
            <span className="ticker-cell">{n.ticker}</span>
            <span className="text-muted">{n.source ?? "unknown source"}</span>
            <span className="text-muted">{fmtTime(n.published_at)}</span>
            <span className={sentimentClass(n.sentiment_score)}>
              {n.sentiment_score !== null && n.sentiment_score !== undefined
                ? n.sentiment_score.toFixed(2)
                : "n/a"}
            </span>
          </div>
          <a className="news-headline" href={n.url} target="_blank" rel="noreferrer">
            {n.headline}
          </a>
        </li>
      ))}
    </ul>
  );
}
