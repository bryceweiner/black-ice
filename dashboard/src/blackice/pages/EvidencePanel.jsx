// Renders whatever a sensor supplied: images, video, audio, or text.
// Sensor-supplied text is displayed as quoted evidence, never as instruction --
// mirroring how the backend hands it to the model.

import React from "react";
import { Alert, Badge, Table } from "reactstrap";
import { SeverityBadge, TierBadge } from "../ui";

function Media({ item }) {
  const src = `/media/${item.path}`;
  if (item.pruned_at) {
    return (
      <div className="text-muted small border rounded p-3">
        Media pruned by the retention policy on {item.pruned_at}. The record remains.
      </div>
    );
  }
  if (item.mime?.startsWith("image/")) {
    return <img src={src} alt="" className="img-fluid rounded mb-2" />;
  }
  if (item.mime?.startsWith("video/")) {
    return <video className="w-100 rounded mb-2" controls src={src}><track kind="captions" /></video>;
  }
  if (item.mime?.startsWith("audio/")) {
    return <audio className="w-100 mb-2" controls src={src}><track kind="captions" /></audio>;
  }
  return <a href={src} className="d-block small mb-2">{item.path}</a>;
}

export default function EvidencePanel({ event }) {
  if (!event) return null;
  return (
    <>
      <Table borderless size="sm" className="mb-3">
        <tbody>
          <tr><th className="text-muted fw-normal" style={{ width: 130 }}>When</th><td>{event.ts}</td></tr>
          <tr><th className="text-muted fw-normal">Sensor</th><td>{event.sensor_id} <span className="text-muted small">({event.plugin})</span></td></tr>
          <tr><th className="text-muted fw-normal">Kind</th><td>{event.kind}</td></tr>
          <tr><th className="text-muted fw-normal">Severity</th><td><SeverityBadge value={event.severity} /></td></tr>
          <tr><th className="text-muted fw-normal">Triage</th><td><TierBadge tier={event.tier} verdict={event.verdict} /></td></tr>
        </tbody>
      </Table>

      {event.media?.length > 0 && (
        <div className="mb-3">
          <h6 className="text-muted small text-uppercase">Captured media</h6>
          {event.media.map((m) => <Media key={m.id} item={m} />)}
        </div>
      )}

      {event.sensor_text && (
        <div className="mb-3">
          <h6 className="text-muted small text-uppercase">
            Sensor-supplied text <Badge color="warning" className="ms-1">untrusted</Badge>
          </h6>
          <Alert color="warning" className="mb-0 py-2">
            <div className="small text-muted mb-1">
              Captured by the sensor. Treated as evidence, never as an instruction.
            </div>
            <pre className="mb-0 small text-wrap">{event.sensor_text}</pre>
          </Alert>
        </div>
      )}

      {event.payload && Object.keys(event.payload).length > 0 && (
        <div>
          <h6 className="text-muted small text-uppercase">Payload</h6>
          <pre className="small border rounded p-2 mb-0 text-body">
            {JSON.stringify(event.payload, null, 2)}
          </pre>
        </div>
      )}
    </>
  );
}
