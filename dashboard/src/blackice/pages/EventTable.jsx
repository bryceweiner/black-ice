import React from "react";
import { Badge, Table } from "reactstrap";

export const SEVERITY = ["info", "low", "medium", "high", "critical"];
const TONE = ["secondary", "info", "warning", "danger", "danger"];

export function SeverityBadge({ value }) {
  const i = Math.max(0, Math.min(4, Number(value) || 0));
  return <Badge color={TONE[i]} className="text-uppercase">{SEVERITY[i]}</Badge>;
}

export function TierBadge({ tier, verdict }) {
  if (!tier) return <span className="text-muted small">pending</span>;
  const tone = tier === "rules" ? "light" : tier === "small_model" ? "info" : "primary";
  return (
    <>
      <Badge color={tone} className={tone === "light" ? "text-dark" : ""}>{tier}</Badge>
      {verdict && <span className="text-muted small ms-2">{verdict}</span>}
    </>
  );
}

export default function EventTable({ rows, onSelect }) {
  if (!rows?.length) {
    return <div className="text-muted text-center py-4">No events match these filters.</div>;
  }
  return (
    <div className="table-responsive">
      <Table hover className="align-middle mb-0">
        <thead>
          <tr>
            <th>When</th>
            <th>Sensor</th>
            <th>Kind</th>
            <th>Summary</th>
            <th>Severity</th>
            <th>Triage</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={r.id}
              onClick={() => onSelect?.(r)}
              style={onSelect ? { cursor: "pointer" } : undefined}
            >
              <td className="text-nowrap small text-muted">{r.ts}</td>
              <td className="small">{r.sensor_id}</td>
              <td className="small">{r.kind}</td>
              <td>{r.summary}</td>
              <td><SeverityBadge value={r.severity} /></td>
              <td><TierBadge tier={r.tier} verdict={r.verdict} /></td>
            </tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}
