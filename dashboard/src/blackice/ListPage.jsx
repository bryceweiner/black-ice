// Search + date-range shell shared by every list (sensors, events,
// escalations, alarms). Written once so the four pages cannot drift apart.

import React, { useCallback, useEffect, useState } from "react";
import { Card, CardBody, CardHeader, Col, Input, Row, Spinner } from "reactstrap";
import DatePicker from "react-datepicker";
import { RefreshCw, Search, X } from "react-feather";

const DEBOUNCE_MS = 250;

export function useListQuery(fetcher, { liveTopic } = {}) {
  const [q, setQ] = useState("");
  const [range, setRange] = useState([null, null]);
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [tick, setTick] = useState(0);

  const [start, end] = range;

  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      setLoading(true);
      try {
        const params = {
          q: q || undefined,
          start: start ? isoDay(start) : undefined,
          // Inclusive end: the user picking a day means "through that day".
          end: end ? `${isoDay(end)} 23:59:59` : undefined,
        };
        const data = await fetcher(params);
        if (cancelled) return;
        const list = Array.isArray(data) ? data : data.rows;
        setRows(list || []);
        setTotal(Array.isArray(data) ? data.length : data.total ?? 0);
        setError(null);
      } catch (e) {
        if (!cancelled) setError(e.message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    const t = setTimeout(run, q ? DEBOUNCE_MS : 0);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [q, start, end, tick, fetcher]);

  return { q, setQ, range, setRange, rows, total, loading, error, reload, liveTopic };
}

function isoDay(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function ListToolbar({ state, placeholder = "Search…", children }) {
  const { q, setQ, range, setRange, total, loading, reload } = state;
  const [start, end] = range;
  const filtered = q || start || end;

  return (
    <Row className="align-items-center g-2 mb-1">
      <Col md={4}>
        <div className="position-relative">
          <Search
            size={16}
            className="position-absolute top-50 translate-middle-y ms-2 text-muted"
          />
          <Input
            className="ps-5"
            value={q}
            placeholder={placeholder}
            aria-label={placeholder}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
      </Col>
      <Col md={4}>
        <DatePicker
          className="form-control"
          selectsRange
          isClearable
          startDate={start}
          endDate={end}
          onChange={setRange}
          dateFormat="yyyy-MM-dd"
          placeholderText="Filter by date range"
        />
      </Col>
      <Col xs="auto" className="text-muted small text-nowrap">
        {loading ? <Spinner size="sm" /> : `${total} result${total === 1 ? "" : "s"}`}
        {filtered && (
          <button
            type="button"
            className="btn btn-link btn-sm p-0 ms-2 align-baseline"
            onClick={() => {
              setQ("");
              setRange([null, null]);
            }}
          >
            <X size={13} /> clear
          </button>
        )}
      </Col>
      <Col className="d-flex justify-content-end align-items-center gap-2 flex-wrap">
        {children}
        <button
          type="button"
          className="btn btn-outline-primary btn-sm"
          onClick={reload}
          aria-label="Refresh"
        >
          <RefreshCw size={14} />
        </button>
      </Col>
    </Row>
  );
}

export default function ListPage({ title, subtitle, state, toolbarExtra, children }) {
  return (
    <Card className="mb-0">
      <CardHeader className="pb-2">
        <div className="d-flex justify-content-between align-items-start mb-3">
          <div>
            <h5 className="mb-0">{title}</h5>
            {subtitle && <small className="text-muted">{subtitle}</small>}
          </div>
        </div>
        <ListToolbar state={state}>{toolbarExtra}</ListToolbar>
      </CardHeader>
      <CardBody className="pt-3">
        {state.error ? (
          <div className="alert alert-danger mb-0">{state.error}</div>
        ) : (
          children
        )}
      </CardBody>
    </Card>
  );
}
