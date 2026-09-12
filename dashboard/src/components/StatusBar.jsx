import { useEffect, useState } from "react";
import { api } from "../api";

export default function StatusBar() {
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [autonomous, setAutonomous] = useState(null);
  const [autonomousBusy, setAutonomousBusy] = useState(false);

  const refresh = () => {
    api
      .status()
      .then((s) => {
        setStatus(s);
        setError(null);
      })
      .catch(() => setError("Backend unreachable"));
    api
      .autonomousStatus()
      .then((a) => setAutonomous(a.autonomous_enabled))
      .catch(() => {});
  };

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 15000);
    return () => clearInterval(id);
  }, []);

  const toggle = async () => {
    setBusy(true);
    try {
      if (status?.bot_enabled) {
        await api.killSwitch();
      } else {
        await api.resume();
      }
      refresh();
    } finally {
      setBusy(false);
    }
  };

  const toggleAutonomous = async () => {
    if (!autonomous) {
      const confirmed = window.confirm(
        "Turn on autonomous trading?\n\nWhile this is on and the dashboard app is running, the bot will " +
          "place and close paper trades on its own — no click needed — whenever a ticker on the watchlist " +
          "clears the entry/exit rules. The kill switch still applies. Turning this off again always stops it."
      );
      if (!confirmed) return;
    }
    setAutonomousBusy(true);
    try {
      if (autonomous) {
        await api.disableAutonomous();
      } else {
        await api.enableAutonomous();
      }
      refresh();
    } finally {
      setAutonomousBusy(false);
    }
  };

  if (error) {
    return (
      <div className="status-bar status-bar-error">
        <span className="status-dot status-dot-critical" />
        {error}
      </div>
    );
  }

  const enabled = status?.bot_enabled ?? false;

  return (
    <div className="status-bar">
      <span className={`status-dot ${enabled ? "status-dot-good" : "status-dot-critical"}`} />
      <span className="status-label">{enabled ? "Bot active" : "Trading halted"}</span>
      {!enabled && status?.kill_switch_reason && (
        <span className="status-reason text-muted">({status.kill_switch_reason})</span>
      )}
      <button
        className={`kill-switch-button ${enabled ? "kill" : "resume"}`}
        onClick={toggle}
        disabled={busy || status === null}
      >
        {enabled ? "Kill Switch" : "Resume Trading"}
      </button>

      <span className={`status-dot ${autonomous ? "status-dot-good" : "status-dot-critical"}`} />
      <span className="status-label">{autonomous ? "Autonomous mode ON" : "Autonomous mode off"}</span>
      <button
        className={`kill-switch-button ${autonomous ? "kill" : "resume"}`}
        onClick={toggleAutonomous}
        disabled={autonomousBusy || autonomous === null}
      >
        {autonomous ? "Turn Off" : "Turn On"}
      </button>
    </div>
  );
}
