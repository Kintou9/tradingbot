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
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const toggleAutonomous = async () => {
    if (!autonomous) {
      const confirmed = window.confirm(
        "Enable autonomous entries?\n\nThe backend will place protected orders within the configured limits. " +
          "Turning this off or pausing entries keeps existing-position protection running. " +
          "The backend must stay running for monitoring and reconciliation."
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
    } catch (err) {
      setError(err.message);
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
      <span className="status-label">{enabled ? "Entries allowed" : "Entries paused — protection continues"}</span>
      {!enabled && status?.kill_switch_reason && (
        <span className="status-reason text-muted">({status.kill_switch_reason})</span>
      )}
      <button
        className={`kill-switch-button ${enabled ? "kill" : "resume"}`}
        onClick={toggle}
        disabled={busy || status === null}
      >
        {enabled ? "Pause Entries" : "Resume Entries"}
      </button>

      <span className="status-label">{status?.trading_mode?.toUpperCase()}</span>
      {status?.monitoring?.issues?.length > 0 && (
        <span className="text-critical" title={status.monitoring.issues.join("\n")}>
          Needs attention: {status.monitoring.issues[0]}
        </span>
      )}
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
