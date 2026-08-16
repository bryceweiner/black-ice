// An escalation is the one thing the assistant raises for a human, and until
// now it only appeared if you happened to be looking at /escalations. This
// surfaces it wherever you are, and clicking takes you to it.

import React from "react";
import { toast } from "react-toastify";
import { useNavigate } from "react-router-dom";
import { AlertTriangle } from "react-feather";
import { useLive } from "./live";
import { THREAT } from "./tokens";

export default function EscalationAlerts() {
  const navigate = useNavigate();

  useLive("escalation", (e) => {
    if (!e || e.status !== "open") return;
    const threat = THREAT[e.threat_level] ?? THREAT.unknown;
    toast(
      <div>
        <div className="fw-semibold d-flex align-items-center gap-2" style={{ color: threat.color }}>
          <AlertTriangle size={15} />
          {threat.label} · {e.classification || "Escalation"}
        </div>
        {e.suggested_action && <div className="small mt-1">{e.suggested_action}</div>}
      </div>,
      {
        // Critical stays until it is dismissed: an alert that disappears on
        // its own is an alert nobody saw.
        autoClose: e.threat_level === "critical" ? false : 12000,
        onClick: () => navigate("/escalations"),
        style: { cursor: "pointer", borderLeft: `3px solid ${threat.color}` },
      }
    );
  });

  return null;
}
