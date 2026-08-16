import React, { useCallback, useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useLive } from "../live";
import ListPage, { useListQuery } from "../ListPage";
import EventTable from "./EventTable";

export default function Events() {
  const fetcher = useCallback((params) => api.events(params), []);
  const state = useListQuery(fetcher);
  const [params] = useSearchParams();
  const q = params.get("q");

  // Header search lands here with ?q=, so the page opens already filtered.
  const { setQ } = state;
  useEffect(() => {
    if (q) setQ(q);
  }, [q, setQ]);

  useLive("event", () => state.reload());

  return (
    <ListPage
      title="Events"
      subtitle="Everything every sensor has reported"
      state={state}
    >
      <EventTable rows={state.rows} />
    </ListPage>
  );
}
