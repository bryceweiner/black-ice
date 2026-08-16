// One sensor: the plugin's declared widgets, its event log, its alarms.
//
// This lives on /sensors/:id rather than in the list's local state, so the
// home screen, search and the assistant can all link straight to a device.

import React, { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Card, CardBody, CardHeader, Col, Nav, NavItem, NavLink, Row,
  TabContent, TabPane,
} from "reactstrap";
import { ChevronLeft } from "react-feather";
import { api } from "../api";
import { useLive } from "../live";
import { ListToolbar, useListQuery } from "../ListPage";
import { Widget } from "../widgets";
import EventTable from "./EventTable";
import { StateBadge } from "./Sensors";

const TABS = ["overview", "events", "alarms"];

export default function SensorDetail() {
  const { id } = useParams();
  const [sensor, setSensor] = useState(null);
  const [tab, setTab] = useState("overview");
  const [error, setError] = useState(null);

  const load = useCallback(
    () => api.sensor(id).then(setSensor).catch((e) => setError(e.message)),
    [id]
  );
  useEffect(() => {
    setSensor(null);
    setError(null);
    load();
  }, [load]);
  useLive("alarm_state", load);
  useLive("sensor_state", (p) => p?.sensor_id === id && load());

  if (error) return <div className="alert alert-danger">{error}</div>;
  if (!sensor) return null;

  const widgets = sensor.descriptor?.widgets ?? [];
  const streams = sensor.descriptor?.streams ?? [];

  return (
    <Card className="mb-0">
      <CardHeader>
        <Link to="/sensors" className="btn btn-link p-0 mb-2 d-inline-flex align-items-center gap-1">
          <ChevronLeft size={15} /> All sensors
        </Link>
        <div className="d-flex justify-content-between align-items-center">
          <div>
            <h5 className="mb-0">{sensor.name}</h5>
            <small className="text-muted">{sensor.id} · {sensor.plugin}</small>
          </div>
          <StateBadge state={sensor.state} />
        </div>
        <Nav tabs className="mt-3 border-bottom-0">
          {TABS.map((t) => (
            <NavItem key={t}>
              <NavLink
                href="#"
                className={tab === t ? "active text-capitalize" : "text-capitalize"}
                onClick={(e) => { e.preventDefault(); setTab(t); }}
              >
                {t}
              </NavLink>
            </NavItem>
          ))}
        </Nav>
      </CardHeader>
      <CardBody>
        <TabContent activeTab={tab}>
          <TabPane tabId="overview">
            <Row>
              {streams.map((s, i) => (
                <Widget key={`stream${i}`} sensorId={sensor.id}
                        spec={{ type: "video", title: s.name || "Live view", props: s, span: 12 }} />
              ))}
              {widgets.map((w, i) => <Widget key={i} sensorId={sensor.id} spec={w} />)}
              {!widgets.length && !streams.length && (
                <Col><p className="text-muted">This plugin declares no widgets.</p></Col>
              )}
            </Row>
          </TabPane>
          <TabPane tabId="events">
            {tab === "events" && <SensorEvents sensorId={sensor.id} />}
          </TabPane>
          <TabPane tabId="alarms">
            {sensor.alarms.length === 0 && <p className="text-muted mb-0">No alarms for this sensor.</p>}
            {sensor.alarms.map((a) => (
              <div key={a.id} className="d-flex justify-content-between align-items-center border-bottom py-2">
                <div>
                  <strong>{a.name}</strong>
                  <div className="text-muted small">{a.description}</div>
                </div>
                <div className="form-check form-switch">
                  <input
                    className="form-check-input"
                    type="checkbox"
                    role="switch"
                    checked={Boolean(a.armed)}
                    aria-label={`Arm ${a.name}`}
                    onChange={(e) => api.setAlarmArmed(a.id, e.target.checked).then(load)}
                  />
                </div>
              </div>
            ))}
          </TabPane>
        </TabContent>
      </CardBody>
    </Card>
  );
}

function SensorEvents({ sensorId }) {
  const fetcher = useCallback((p) => api.events({ ...p, sensor_id: sensorId }), [sensorId]);
  const state = useListQuery(fetcher);
  useLive("event", (e) => e?.sensor_id === sensorId && state.reload());
  return (
    <>
      <div className="mb-3">
        <ListToolbar state={state} placeholder="Search this sensor's events…" />
      </div>
      <EventTable rows={state.rows} />
    </>
  );
}
