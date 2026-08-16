import React, { useCallback, useState } from "react";
import {
  Button, ButtonGroup, Card, CardBody, Col, Input, Modal, ModalBody,
  ModalHeader, Row,
} from "reactstrap";
import { AlertTriangle, CheckCircle, HelpCircle } from "react-feather";
import { toast } from "react-toastify";
import { api } from "../api";
import { useLive } from "../live";
import ListPage, { useListQuery } from "../ListPage";
import { Rows, StatusPill, ThreatBadge } from "../ui";
import { THREAT_ORDER } from "../tokens";
import EvidencePanel from "./EvidencePanel";

const COLUMNS = [
  {
    name: "When",
    selector: (r) => r.ts,
    sortable: true,
    width: "170px",
    cell: (r) => <span className="small text-muted text-nowrap">{r.ts}</span>,
  },
  {
    name: "Threat",
    // Sorts by severity, not alphabetically: "critical" above "low" is the
    // whole point of the column.
    selector: (r) => THREAT_ORDER.indexOf(r.threat_level),
    sortable: true,
    width: "130px",
    cell: (r) => <ThreatBadge level={r.threat_level} />,
  },
  { name: "Classification", selector: (r) => r.classification, sortable: true, wrap: true },
  {
    name: "Suggested action",
    selector: (r) => r.suggested_action,
    wrap: true,
    grow: 2,
    cell: (r) => <span className="small text-muted">{r.suggested_action}</span>,
  },
  {
    name: "Status",
    selector: (r) => r.status,
    sortable: true,
    width: "150px",
    cell: (r) => <StatusPill status={r.status} />,
  },
];

export default function Escalations() {
  const fetcher = useCallback((params) => api.escalations(params), []);
  const state = useListQuery(fetcher);
  const [detail, setDetail] = useState(null);

  useLive("escalation", () => state.reload());

  const open = async (id) => setDetail(await api.escalation(id));

  return (
    <>
      <ListPage
        title="Escalations"
        subtitle="Events the assistant raised for your attention"
        state={state}
      >
        <Rows
          columns={COLUMNS}
          rows={state.rows}
          onRowClick={(r) => open(r.id)}
          empty="Nothing needs your attention."
        />
      </ListPage>

      <Modal isOpen={Boolean(detail)} toggle={() => setDetail(null)} size="lg" scrollable>
        <ModalHeader toggle={() => setDetail(null)}>
          {detail && <ThreatBadge level={detail.threat_level} />}{" "}
          {detail?.classification}
        </ModalHeader>
        <ModalBody>
          {detail && (
            <EscalationDetail
              detail={detail}
              onChange={async () => {
                setDetail(await api.escalation(detail.id));
                state.reload();
              }}
            />
          )}
        </ModalBody>
      </Modal>
    </>
  );
}

function EscalationDetail({ detail, onChange }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const verdict = async (value) => {
    setBusy(true);
    try {
      await api.addVerdict(detail.id, value, note || undefined);
      setNote("");
      await onChange();
    } catch (e) {
      toast.error(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Row className="mb-3">
        <Col md={8}>
          <h6 className="text-muted small text-uppercase">Assistant's reasoning</h6>
          <p className="mb-3">{detail.reasoning}</p>
          <h6 className="text-muted small text-uppercase">Suggested course of action</h6>
          <Card className="border mb-0">
            <CardBody className="py-2 text-body">
              {detail.suggested_action || "None."}
            </CardBody>
          </Card>
        </Col>
        <Col md={4}>
          <h6 className="text-muted small text-uppercase">Reporting sensor</h6>
          <p className="mb-1">
            <strong>{detail.sensor?.name ?? "—"}</strong>
            <br />
            <span className="text-muted small">{detail.sensor?.id}</span>
          </p>
          <div className="text-muted small">
            classified by {detail.model}
            {detail.prompt_version && ` · prompt v${detail.prompt_version}`}
          </div>
          <ButtonGroup size="sm" className="mt-3 flex-wrap">
            {["open", "acknowledged", "resolved"].map((s) => (
              <Button
                key={s}
                outline={detail.status !== s}
                color="primary"
                onClick={() => api.setEscalationStatus(detail.id, s).then(onChange)}
              >
                {s}
              </Button>
            ))}
          </ButtonGroup>
        </Col>
      </Row>

      <hr />
      <h6 className="text-muted small text-uppercase">Sensor event</h6>
      <EvidencePanel event={detail.event} />

      <hr />
      <h6 className="text-muted small text-uppercase">Was this right?</h6>
      <p className="text-muted small">
        Your answer is remembered and used as precedent when this sensor reports again.
      </p>
      <Input
        className="mb-2"
        placeholder="Optional note — e.g. 'that is the postman'"
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      <div className="d-flex gap-2">
        <Button color="success" size="sm" disabled={busy} onClick={() => verdict("true_positive")}>
          <CheckCircle size={14} /> Worth escalating
        </Button>
        <Button color="warning" size="sm" disabled={busy} onClick={() => verdict("false_positive")}>
          <AlertTriangle size={14} /> False positive
        </Button>
        <Button color="secondary" size="sm" disabled={busy} onClick={() => verdict("unclear")}>
          <HelpCircle size={14} /> Unclear
        </Button>
      </div>

      {detail.verdicts?.length > 0 && (
        <ul className="list-unstyled small text-muted mt-3 mb-0">
          {detail.verdicts.map((v) => (
            <li key={v.id}>
              {v.ts} — <strong>{v.verdict}</strong>
              {v.note && ` · ${v.note}`}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
