import React, { useCallback, useState } from "react";
import { Modal, ModalBody, ModalHeader } from "reactstrap";
import { api } from "../api";
import { useLive } from "../live";
import ListPage, { useListQuery } from "../ListPage";
import EventTable from "./EventTable";
import EvidencePanel from "./EvidencePanel";

export default function Events() {
  const fetcher = useCallback((params) => api.events(params), []);
  const state = useListQuery(fetcher);
  const [detail, setDetail] = useState(null);

  useLive("event", () => state.reload());

  const open = async (row) => {
    try {
      setDetail(await api.event(row.id));
    } catch (e) {
      alert(e.message);
    }
  };

  return (
    <>
      <ListPage
        title="Events"
        subtitle="Everything every sensor has reported"
        state={state}
      >
        <EventTable rows={state.rows} onSelect={open} />
      </ListPage>

      <Modal isOpen={Boolean(detail)} toggle={() => setDetail(null)} size="lg" scrollable>
        <ModalHeader toggle={() => setDetail(null)}>
          {detail?.summary || "Event"}
        </ModalHeader>
        <ModalBody>{detail && <EvidencePanel event={detail} />}</ModalBody>
      </Modal>
    </>
  );
}
