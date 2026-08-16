import React, { useCallback, useState } from "react";
import { Badge, Button } from "reactstrap";
import { api } from "../api";
import { useLive } from "../live";
import ListPage, { useListQuery } from "../ListPage";

export default function Alarms() {
  // Alarms carry no timestamp of their own, so the range filter applies to
  // when each was last toggled.
  const fetcher = useCallback((params) => api.alarms({ q: params.q }), []);
  const state = useListQuery(fetcher);
  const [busy, setBusy] = useState(null);

  useLive("alarm_state", () => state.reload());

  const toggle = async (rule, armed) => {
    setBusy(rule.id);
    try {
      await api.setAlarmArmed(rule.id, armed);
      state.reload();
    } catch (e) {
      alert(e.message);
    } finally {
      setBusy(null);
    }
  };

  const byPlugin = state.rows.reduce((acc, r) => {
    (acc[r.plugin] ||= []).push(r);
    return acc;
  }, {});

  const armedCount = state.rows.filter((r) => r.armed).length;

  return (
    <ListPage
      title="Alarms"
      subtitle={`${armedCount} of ${state.rows.length} armed`}
      state={state}
      toolbarExtra={
        <>
          <Button size="sm" color="primary" outline
                  onClick={() => api.setAllAlarms(true).then(state.reload)}>
            Arm all
          </Button>
          <Button size="sm" color="secondary" outline className="ms-1"
                  onClick={() => api.setAllAlarms(false).then(state.reload)}>
            Disarm all
          </Button>
        </>
      }
    >
      {state.rows.length === 0 ? (
        <div className="text-muted text-center py-4">
          No alarms. Sensor plugins declare these.
        </div>
      ) : (
        Object.entries(byPlugin).map(([plugin, rules]) => (
          <div key={plugin} className="mb-3">
            <h6 className="text-muted small text-uppercase">{plugin}</h6>
            <div className="list-group">
              {rules.map((r) => (
                <div key={r.id} className="list-group-item d-flex justify-content-between align-items-center">
                  <div>
                    <strong>{r.name}</strong>
                    {r.sensor_name && (
                      <Badge color="light" className="text-dark ms-2">{r.sensor_name}</Badge>
                    )}
                    <div className="text-muted small">{r.description}</div>
                    {r.updated_at && (
                      <div className="text-muted" style={{ fontSize: ".75rem" }}>
                        last changed {r.updated_at} by {r.updated_by}
                      </div>
                    )}
                  </div>
                  <div className="form-check form-switch">
                    <input
                      className="form-check-input"
                      type="checkbox"
                      role="switch"
                      aria-label={`Arm ${r.name}`}
                      disabled={busy === r.id}
                      checked={Boolean(r.armed)}
                      onChange={(e) => toggle(r, e.target.checked)}
                    />
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))
      )}
    </ListPage>
  );
}
