import React, { useCallback, useEffect, useState } from "react";
import { Badge, Card, CardBody, CardHeader, Table } from "reactstrap";
import { api } from "../api";
import { useLive } from "../live";

const TONE = { healthy: "success", unhealthy: "danger", stopped: "secondary" };

export default function Plugins() {
  const [rows, setRows] = useState([]);
  const [error, setError] = useState(null);

  const load = useCallback(
    () => api.plugins().then(setRows).catch((e) => setError(e.message)),
    []
  );
  useEffect(() => { load(); }, [load]);
  useLive("plugin_health", load);

  return (
    <Card className="mb-0">
      <CardHeader>
        <h5 className="mb-0">Plugins</h5>
        <small className="text-muted">
          A failing plugin degrades to a badge here rather than stopping the service.
        </small>
      </CardHeader>
      <CardBody>
        {error && <div className="alert alert-danger">{error}</div>}
        {rows.length === 0 ? (
          <div className="text-muted text-center py-4">No plugins installed.</div>
        ) : (
          <Table className="align-middle mb-0">
            <thead>
              <tr><th>Plugin</th><th>Version</th><th>State</th><th>Restarts</th><th>Last error</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.plugin}>
                  <td><strong>{r.plugin}</strong></td>
                  <td className="small text-muted">{r.version}</td>
                  <td>
                    <Badge color={TONE[r.state] ?? "secondary"} className="text-uppercase">
                      {r.state}
                    </Badge>
                  </td>
                  <td>{r.restarts}</td>
                  <td className="small text-danger">{r.last_error ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </CardBody>
    </Card>
  );
}
