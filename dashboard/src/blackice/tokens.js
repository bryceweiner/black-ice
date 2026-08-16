// One place for every colour that carries meaning.
//
// Bootstrap's contextual names (`light`, `secondary`) were doing this job
// before, and they broke in two ways: `badge bg-light` is white-on-white in
// dark mode, and `secondary` is now amber, so "unknown" shouted louder than
// "critical". These are semantic tokens instead -- always rendered beside a
// written label, never carrying the meaning alone.

// Severity of a threat, in order. Fixed meanings, not series identity.
export const THREAT = {
  critical: { color: "#f43f5e", label: "Critical" },
  high: { color: "#fb923c", label: "High" },
  elevated: { color: "#fb923c", label: "Elevated" },
  medium: { color: "#facc15", label: "Medium" },
  low: { color: "#22c55e", label: "Low" },
  benign: { color: "#22c55e", label: "Benign" },
  unknown: { color: "#64748b", label: "Unknown" },
};
export const THREAT_ORDER = ["critical", "high", "elevated", "medium", "low", "benign", "unknown"];

// Event severity is an integer 0-4 from the plugin.
export const SEVERITY = [
  { color: "#64748b", label: "Info" },
  { color: "#38bdf8", label: "Low" },
  { color: "#facc15", label: "Medium" },
  { color: "#fb923c", label: "High" },
  { color: "#f43f5e", label: "Critical" },
];

// Where an event's verdict came from. Identity, not severity: a rules hit is
// not "less bad" than a model call, it is a different path.
export const TIER = {
  rules: { color: "#64748b", label: "rules" },
  small_model: { color: "#22d3ee", label: "small model" },
  primary: { color: "#38bdf8", label: "primary" },
  unknown: { color: "#64748b", label: "unknown" },
};

export const ESCALATION_STATUS = {
  open: { color: "#fb923c", label: "Open" },
  acknowledged: { color: "#38bdf8", label: "Acknowledged" },
  resolved: { color: "#22c55e", label: "Resolved" },
};

export const SENSOR_STATE = {
  online: "#22c55e",
  offline: "#f43f5e",
  unknown: "#64748b",
};

export function pluginTone(state) {
  if (["healthy", "running", "started"].includes(state)) return "#22c55e";
  if (["starting", "restarting", "degraded"].includes(state)) return "#facc15";
  if (state === "stopped") return "#64748b";
  return "#f43f5e";
}

// Chart furniture, shared so every plot in the app matches.
export const CHART = {
  series: "#38bdf8",
  grid: "rgba(148,163,184,.16)",
  axis: "#7d8b9a",
  surface: "#131a22",
};
