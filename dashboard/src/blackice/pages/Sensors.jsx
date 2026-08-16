// Sensors list with collapsible groups, plus a detail view showing the
// plugin's declared widgets, its event log, and its alarm configuration.

import React, { useCallback, useEffect, useState } from "react";
import {
  Badge, Button, Card, CardBody, CardHeader, Col, Collapse, Input,
  Modal, ModalBody, ModalFooter, ModalHeader, Nav, NavItem, NavLink, Row,
  TabContent, TabPane,
} from "reactstrap";
import { ChevronDown, ChevronRight, Folder, Plus, Trash2, X } from "react-feather";
import { api } from "../api";
import { useLive } from "../live";
import ListPage, { ListToolbar, useListQuery } from "../ListPage";
import { Widget } from "../widgets";
import EventTable from "./EventTable";

function StateBadge({ state }) {
  const tone = { online: "success", offline: "danger", unknown: "secondary" }[state] ?? "secondary";
  return <Badge color={tone} pill className="text-uppercase small">{state}</Badge>;
}

export default function Sensors() {
  const fetcher = useCallback((params) => api.sensors(params), []);
  const state = useListQuery(fetcher);
  const [groups, setGroups] = useState([]);
  const [open, setOpen] = useState({});
  const [selected, setSelected] = useState(null);
  const [newGroup, setNewGroup] = useState("");
  const [assigning, setAssigning] = useState(null);

  const loadGroups = useCallback(() => api.groups().then(setGroups).catch(() => {}), []);
  useEffect(() => { loadGroups(); }, [loadGroups]);

  // Live: a plugin coming up or a group change refreshes both lists.
  useLive("sensor_state", () => { state.reload(); loadGroups(); });

  const grouped = new Set(groups.flatMap((g) => g.sensors.map((s) => s.id)));
  const ungrouped = state.rows.filter((s) => !grouped.has(s.id));

  const createGroup = async () => {
    if (!newGroup.trim()) return;
    try {
      await api.createGroup(newGroup.trim());
      setNewGroup("");
      loadGroups();
    } catch (e) {
      alert(e.message);
    }
  };

  if (selected) return <SensorDetail id={selected} onBack={() => setSelected(null)} />;

  return (
    <>
      <ListPage
        title="Sensors"
        subtitle="Grouped devices reporting into the system"
        state={state}
        toolbarExtra={
          <div className="d-inline-flex align-items-center gap-1">
            <Input
              bsSize="sm"
              style={{ width: 160 }}
              placeholder="New group name"
              value={newGroup}
              onChange={(e) => setNewGroup(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && createGroup()}
            />
            <Button size="sm" color="primary" onClick={createGroup}>
              <Plus size={14} /> Group
            </Button>
          </div>
        }
      >
        {groups.map((g) => {
          const members = g.sensors.filter((s) =>
            state.rows.some((r) => r.id === s.id)
          );
          const isOpen = open[g.id] ?? !g.collapsed;
          return (
            <div key={g.id} className="mb-2 border rounded">
              <div
                className="d-flex align-items-center justify-content-between px-3 py-2 border-bottom"
                role="button"
                tabIndex={0}
                onClick={() => {
                  setOpen({ ...open, [g.id]: !isOpen });
                  api.updateGroup(g.id, { collapsed: isOpen }).catch(() => {});
                }}
                onKeyDown={(e) => e.key === "Enter" && setOpen({ ...open, [g.id]: !isOpen })}
              >
                <span className="fw-semibold">
                  {isOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                  <Folder size={14} className="mx-2" />
                  {g.name}
                  <Badge color="light" className="text-dark ms-2">{members.length}</Badge>
                </span>
                <span onClick={(e) => e.stopPropagation()}>
                  <Button size="sm" color="link" onClick={() => setAssigning(g)}>
                    <Plus size={14} />
                  </Button>
                  <Button
                    size="sm"
                    color="link"
                    className="text-danger"
                    onClick={() => api.deleteGroup(g.id).then(loadGroups)}
                  >
                    <Trash2 size={14} />
                  </Button>
                </span>
              </div>
              <Collapse isOpen={isOpen}>
                <SensorRows
                  rows={members}
                  onSelect={setSelected}
                  onRemove={(sid) => api.removeFromGroup(g.id, sid).then(loadGroups)}
                />
              </Collapse>
            </div>
          );
        })}

        <div className="mt-3">
          {groups.length > 0 && <h6 className="text-muted small">Ungrouped</h6>}
          <SensorRows rows={ungrouped} onSelect={setSelected} />
          {!state.loading && state.rows.length === 0 && (
            <div className="text-muted text-center py-4">
              No sensors yet. Install a sensor plugin to get started.
            </div>
          )}
        </div>
      </ListPage>

      <Modal isOpen={Boolean(assigning)} toggle={() => setAssigning(null)}>
        <ModalHeader toggle={() => setAssigning(null)}>
          Add sensors to {assigning?.name}
        </ModalHeader>
        <ModalBody>
          {ungrouped.length === 0 && <p className="text-muted mb-0">All sensors are grouped.</p>}
          {ungrouped.map((s) => (
            <Button
              key={s.id}
              color="light"
              className="me-2 mb-2"
              onClick={() => api.addToGroup(assigning.id, s.id).then(loadGroups)}
            >
              {s.name}
            </Button>
          ))}
        </ModalBody>
        <ModalFooter>
          <Button color="secondary" onClick={() => setAssigning(null)}>Done</Button>
        </ModalFooter>
      </Modal>
    </>
  );
}

function SensorRows({ rows, onSelect, onRemove }) {
  if (!rows.length) return <div className="text-muted small px-3 py-2">Empty</div>;
  return (
    <div className="list-group list-group-flush">
      {rows.map((s) => (
        <div key={s.id} className="list-group-item d-flex align-items-center justify-content-between">
          <button type="button" className="btn btn-link p-0 text-start" onClick={() => onSelect(s.id)}>
            <strong>{s.name}</strong>
            <span className="text-muted small ms-2">{s.plugin} · {s.kind}</span>
          </button>
          <span>
            <StateBadge state={s.state} />
            {onRemove && (
              <Button size="sm" color="link" className="text-muted" onClick={() => onRemove(s.id)}>
                <X size={14} />
              </Button>
            )}
          </span>
        </div>
      ))}
    </div>
  );
}

function SensorDetail({ id, onBack }) {
  const [sensor, setSensor] = useState(null);
  const [tab, setTab] = useState("overview");
  const [error, setError] = useState(null);

  const load = useCallback(
    () => api.sensor(id).then(setSensor).catch((e) => setError(e.message)),
    [id]
  );
  useEffect(() => { load(); }, [load]);
  useLive("alarm_state", load);

  if (error) return <div className="alert alert-danger">{error}</div>;
  if (!sensor) return null;

  const widgets = sensor.descriptor?.widgets ?? [];
  const streams = sensor.descriptor?.streams ?? [];

  return (
    <Card className="mb-0">
      <CardHeader>
        <Button color="link" className="p-0 mb-2" onClick={onBack}>← All sensors</Button>
        <div className="d-flex justify-content-between align-items-center">
          <div>
            <h5 className="mb-0">{sensor.name}</h5>
            <small className="text-muted">{sensor.id} · {sensor.plugin}</small>
          </div>
          <StateBadge state={sensor.state} />
        </div>
        <Nav tabs className="mt-3 border-bottom-0">
          {["overview", "events", "alarms"].map((t) => (
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
