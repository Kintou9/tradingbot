import { useEffect, useState } from "react";
import { api } from "../api";

export default function StatusBar() {
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const refresh = () => {
    api
      .status()
      .then((s) => {
        setStatus(s);
        setError(null);
      })
      .catch(() => setError("Backend unreachable"));
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
    </div>
  );
}
