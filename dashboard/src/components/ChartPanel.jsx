import { useEffect, useRef, useState } from "react";
import { createChart, CandlestickSeries } from "lightweight-charts";
import { api } from "../api";

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function latestByModule(research, module) {
  return research.find((r) => r.module === module) ?? null;
}

export default function ChartPanel({ ticker }) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef(null);
  const [error, setError] = useState(null);
  const [research, setResearch] = useState([]);

  // Create the chart once per mount.
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { color: cssVar("--surface-1") },
        textColor: cssVar("--text-secondary"),
      },
      grid: {
        vertLines: { color: cssVar("--border") },
        horzLines: { color: cssVar("--border") },
      },
      timeScale: { borderColor: cssVar("--border") },
      rightPriceScale: { borderColor: cssVar("--border") },
      autoSize: true,
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor: cssVar("--status-good"),
      downColor: cssVar("--status-critical"),
      borderVisible: false,
      wickUpColor: cssVar("--status-good"),
      wickDownColor: cssVar("--status-critical"),
    });

    chartRef.current = chart;
    seriesRef.current = series;

    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // Load bars + research whenever the selected ticker changes. Guarded
  // against out-of-order responses: switching tickers quickly can let an
  // older ticker's fetch resolve after the newer one, which would
  // otherwise overwrite the chart with stale data.
  useEffect(() => {
    if (!ticker) return;
    setError(null);
    let cancelled = false;

    api
      .chart(ticker, 150)
      .then((d) => {
        if (cancelled || !seriesRef.current) return;
        seriesRef.current.setData(
          d.bars.map((b) => ({
            time: b.date,
            open: b.open,
            high: b.high,
            low: b.low,
            close: b.close,
          }))
        );
        chartRef.current?.timeScale().fitContent();
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      });

    api
      .research(ticker)
      .then((d) => {
        if (!cancelled) setResearch(d.research);
      })
      .catch(() => {
        if (!cancelled) setResearch([]);
      });

    return () => {
      cancelled = true;
    };
  }, [ticker]);

  const technical = latestByModule(research, "technical_scan");
  const dcf = latestByModule(research, "dcf");
  const deepDive = latestByModule(research, "deep_dive");

  return (
    <div className="chart-panel">
      <div className="chart-container" ref={containerRef} />
      {error && <div className="panel-error">Failed to load chart: {error}</div>}

      <div className="research-cards">
        <div className="research-card">
          <h4>Technical</h4>
          {technical ? (
            <>
              <div className={`verdict-pill trend-${technical.structured_output?.trend}`}>
                {technical.structured_output?.trend ?? "—"}
              </div>
              <dl>
                <dt>Entry</dt>
                <dd>{technical.structured_output?.entry_price ?? "—"}</dd>
                <dt>Stop</dt>
                <dd>{technical.structured_output?.stop_loss ?? "—"}</dd>
                <dt>Target 1</dt>
                <dd>{technical.structured_output?.target_1 ?? "—"}</dd>
                <dt>Probability</dt>
                <dd>{technical.structured_output?.probability_estimate ?? "—"}%</dd>
              </dl>
            </>
          ) : (
            <div className="text-muted">No cached technical scan.</div>
          )}
        </div>

        <div className="research-card">
          <h4>DCF Valuation</h4>
          {dcf ? (
            <dl>
              <dt>Fair Value</dt>
              <dd>${dcf.structured_output?.estimated_fair_value_per_share ?? "—"}</dd>
              <dt>WACC</dt>
              <dd>{dcf.structured_output?.wacc ?? "—"}</dd>
              <dt>Terminal Growth</dt>
              <dd>{dcf.structured_output?.terminal_growth_rate ?? "—"}</dd>
            </dl>
          ) : (
            <div className="text-muted">No cached DCF.</div>
          )}
        </div>

        <div className="research-card">
          <h4>Deep Dive</h4>
          {deepDive ? (
            <>
              <div className={`verdict-pill verdict-${deepDive.verdict}`}>{deepDive.verdict}</div>
              <div className="text-muted">Confidence: {Math.round((deepDive.confidence ?? 0) * 100)}%</div>
            </>
          ) : (
            <div className="text-muted">No cached deep dive.</div>
          )}
        </div>
      </div>
    </div>
  );
}
