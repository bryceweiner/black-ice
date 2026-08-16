// The event log, sortable and paged, with the evidence for a row one click
// away in the row itself rather than behind a modal.

import React, { useEffect, useState } from "react";
import { Spinner } from "reactstrap";
import { api } from "../api";
import { Rows, SeverityBadge, TierBadge } from "../ui";
import EvidencePanel from "./EvidencePanel";

const COLUMNS = [
  {
    name: "When",
    selector: (r) => r.ts,
    sortable: true,
    width: "170px",
    cell: (r) => <span className="small text-muted text-nowrap">{r.ts}</span>,
  },
  { name: "Sensor", selector: (r) => r.sensor_id, sortable: true, width: "150px",
    cell: (r) => <span className="small">{r.sensor_id}</span> },
  { name: "Kind", selector: (r) => r.kind, sortable: true, width: "120px",
    cell: (r) => <span className="small">{r.kind}</span> },
  { name: "Summary", selector: (r) => r.summary, wrap: true, grow: 2 },
  {
    name: "Severity",
    selector: (r) => Number(r.severity) || 0,
    sortable: true,
    width: "120px",
    cell: (r) => <SeverityBadge value={r.severity} />,
  },
  {
    name: "Triage",
    selector: (r) => r.tier ?? "",
    sortable: true,
    width: "210px",
    cell: (r) => <TierBadge tier={r.tier} verdict={r.verdict} />,
  },
];

// The list query returns columns, not media -- the captures come from the
// per-event endpoint, so expanding a row fetches it.
function Expanded({ data }) {
  const [event, setEvent] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    api.event(data.id)
      .then((e) => !cancelled && setEvent(e))
      .catch((e) => !cancelled && setError(e.message));
    return () => { cancelled = true; };
  }, [data.id]);

  return (
    <div className="px-4 py-3 border-top">
      {error ? (
        <div className="text-danger small">{error}</div>
      ) : event ? (
        <EvidencePanel event={event} />
      ) : (
        <Spinner size="sm" className="text-muted" />
      )}
    </div>
  );
}

export default function EventTable({ rows }) {
  return (
    <Rows
      columns={COLUMNS}
      rows={rows ?? []}
      expandable={Expanded}
      empty="No events match these filters."
    />
  );
}
