// Small shared pieces: a pill that reads on any surface, and the sortable
// table every list page uses.

import React from "react";
import DataTable, { createTheme } from "react-data-table-component";
import { ESCALATION_STATUS, SEVERITY, THREAT, TIER } from "./tokens";

// A tinted pill rather than a solid one: solid status blocks at this density
// read as a wall of colour, and the light-coloured ones lose their text.
export function Pill({ color, children, title }) {
  return (
    <span
      className="d-inline-flex align-items-center gap-1 rounded-pill px-2 py-1"
      title={title}
      style={{
        color,
        background: `color-mix(in srgb, ${color} 16%, transparent)`,
        border: `1px solid color-mix(in srgb, ${color} 35%, transparent)`,
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: ".04em",
        lineHeight: 1.4,
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  );
}

export function ThreatBadge({ level }) {
  const t = THREAT[level] ?? THREAT.unknown;
  return <Pill color={t.color}>{t.label.toUpperCase()}</Pill>;
}

export function SeverityBadge({ value }) {
  const s = SEVERITY[Math.max(0, Math.min(SEVERITY.length - 1, Number(value) || 0))];
  return <Pill color={s.color}>{s.label.toUpperCase()}</Pill>;
}

export function TierBadge({ tier, verdict }) {
  if (!tier) return <span className="text-muted small">pending</span>;
  const t = TIER[tier] ?? TIER.unknown;
  return (
    <span className="d-inline-flex align-items-center gap-2">
      <Pill color={t.color}>{t.label}</Pill>
      {verdict && <span className="text-muted small">{verdict}</span>}
    </span>
  );
}

export function StatusPill({ status }) {
  const s = ESCALATION_STATUS[status] ?? { color: "#64748b", label: status };
  return <Pill color={s.color}>{String(s.label).toUpperCase()}</Pill>;
}

// The table's own dark theme -- its default light one ignores the body class.
createTheme(
  "blackice",
  {
    text: { primary: "rgba(233,241,248,.78)", secondary: "#7d8b9a" },
    background: { default: "transparent" },
    context: { background: "#131a22", text: "#e9f1f8" },
    divider: { default: "rgba(148,163,184,.16)" },
    highlightOnHover: { default: "rgba(56,189,248,.07)", text: "#e9f1f8" },
    striped: { default: "rgba(148,163,184,.04)", text: "rgba(233,241,248,.78)" },
    selected: { default: "rgba(56,189,248,.12)", text: "#e9f1f8" },
    button: { default: "#7d8b9a", hover: "#38bdf8", focus: "#38bdf8", disabled: "#3a4652" },
    sortFocus: { default: "#38bdf8" },
  },
  "dark"
);

const STYLES = {
  headCells: {
    style: {
      fontSize: 11,
      letterSpacing: ".08em",
      textTransform: "uppercase",
      color: "#7d8b9a",
    },
  },
  rows: { style: { minHeight: 52 } },
  pagination: { style: { borderTop: "1px solid rgba(148,163,184,.16)" } },
};

export function Rows({ columns, rows, onRowClick, expandable, empty = "Nothing here yet." }) {
  return (
    <DataTable
      theme="blackice"
      columns={columns}
      data={rows}
      customStyles={STYLES}
      highlightOnHover
      pointerOnHover={Boolean(onRowClick)}
      onRowClicked={onRowClick}
      pagination
      paginationPerPage={25}
      paginationRowsPerPageOptions={[25, 50, 100]}
      expandableRows={Boolean(expandable)}
      expandableRowsComponent={expandable}
      noDataComponent={<div className="text-muted text-center py-4">{empty}</div>}
      persistTableHead
    />
  );
}
