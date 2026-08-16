import React, { useCallback, useState } from "react";
import {
  Badge, Button, ButtonGroup, Card, CardBody, Col, Input, Modal, ModalBody,
  ModalHeader, Row, Table,
} from "reactstrap";
import { AlertTriangle, CheckCircle, HelpCircle } from "react-feather";
import { api } from "../api";
import { useLive } from "../live";
import ListPage, { useListQuery } from "../ListPage";
import EvidencePanel from "./EvidencePanel";

const THREAT_TONE = {
  benign: "success", low: "info", elevated: "warning",
  high: "danger", critical: "danger", unknown: "secondary",
};

export function ThreatBadge({ level }) {
  return (
    <Badge color={THREAT_TONE[level] ?? "secondary"} className="text-uppercase">
      {level}
    </Badge>
  );
}

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
        {state.rows.length === 0 ? (
          <div className="text-muted text-center py-4">Nothing needs your attention.</div>
        ) : (
          <div className="table-responsive">
            <Table hover className="align-middle mb-0">
              <thead>
                <tr>
                  <th>When</th><th>Threat</th><th>Classification</th>
                  <th>Suggested action</th><th>Status</th>
                </tr>
              </thead>
              <tbody>
                {state.rows.map((r) => (
                  <tr key={r.id} style={{ cursor: "pointer" }} onClick={() => open(r.id)}>
                    <td className="text-nowrap small text-muted">{r.ts}</td>
                    <td><ThreatBadge level={r.threat_level} /></td>
                    <td>{r.classification}</td>
                    <td className="small text-muted">{r.suggested_action}</td>
                    <td><Badge color="light" className="text-dark">{r.status}</Badge></td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </div>
        )}
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
      alert(e.message);
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
                color="secondary"
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
