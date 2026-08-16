// Sensors list with collapsible groups, plus a detail view showing the
// plugin's declared widgets, its event log, and its alarm configuration.

import React, { useCallback, useEffect, useState } from "react";
import {
  Badge, Button, Collapse, Input, Modal, ModalBody, ModalFooter, ModalHeader,
} from "reactstrap";
import Swal from "sweetalert2";
import { Link } from "react-router-dom";
import { ChevronDown, ChevronRight, Folder, Plus, Trash2, X } from "react-feather";
import { toast } from "react-toastify";
import { api } from "../api";
import { Pill } from "../ui";
import { SENSOR_STATE } from "../tokens";
import { useLive } from "../live";
import ListPage, { useListQuery } from "../ListPage";

export function StateBadge({ state }) {
  return <Pill color={SENSOR_STATE[state] ?? SENSOR_STATE.unknown}>{String(state).toUpperCase()}</Pill>;
}

export default function Sensors() {
  const fetcher = useCallback((params) => api.sensors(params), []);
  const state = useListQuery(fetcher);
  const [groups, setGroups] = useState([]);
  const [open, setOpen] = useState({});
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
      toast.error(e.message);
    }
  };

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
                  <span className="text-muted small ms-2">{members.length}</span>
                </span>
                <span onClick={(e) => e.stopPropagation()}>
                  <Button size="sm" color="link" onClick={() => setAssigning(g)}>
                    <Plus size={14} />
                  </Button>
                  <Button
                    size="sm"
                    color="link"
                    className="text-danger"
                    onClick={() => confirmDeleteGroup(g, loadGroups)}
                  >
                    <Trash2 size={14} />
                  </Button>
                </span>
              </div>
              <Collapse isOpen={isOpen}>
                <SensorRows
                  rows={members}
                  onRemove={(sid) => api.removeFromGroup(g.id, sid).then(loadGroups)}
                />
              </Collapse>
            </div>
          );
        })}

        <div className="mt-3">
          {groups.length > 0 && <h6 className="text-muted small">Ungrouped</h6>}
          <SensorRows rows={ungrouped} />
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

// Deleting a group is one click next to a chevron, and it takes the grouping
// with it. Ask first.
async function confirmDeleteGroup(group, done) {
  const { isConfirmed } = await Swal.fire({
    title: `Delete "${group.name}"?`,
    text: "The sensors stay; only the grouping is removed.",
    icon: "warning",
    showCancelButton: true,
    confirmButtonText: "Delete group",
    confirmButtonColor: "#f43f5e",
    cancelButtonColor: "#334155",
    background: "#131a22",
    color: "#e9f1f8",
  });
  if (!isConfirmed) return;
  try {
    await api.deleteGroup(group.id);
    done();
  } catch (e) {
    Swal.fire({ icon: "error", title: "Could not delete", text: e.message,
                background: "#131a22", color: "#e9f1f8" });
  }
}

function SensorRows({ rows, onRemove }) {
  if (!rows.length) return <div className="text-muted small px-3 py-2">Empty</div>;
  return (
    <div className="list-group list-group-flush">
      {rows.map((s) => (
        <div key={s.id} className="list-group-item d-flex align-items-center justify-content-between">
          <Link to={`/sensors/${encodeURIComponent(s.id)}`} className="text-decoration-none">
            <strong>{s.name}</strong>
            <span className="text-muted small ms-2">{s.plugin} · {s.kind}</span>
          </Link>
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
