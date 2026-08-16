// The screen you leave open. It answers, without a click: is the system up,
// is anything wrong, what has been happening, and what is each sensor doing --
// with the assistant right there to ask about any of it.
//
// Everything on it is one /api/overview round trip plus the websocket; the
// panels never poll.

import React, { useCallback, useEffect, useRef, useState } from "react";
import { Link, useOutletContext } from "react-router-dom";
import { Card, CardBody, CardHeader, Col, Row } from "reactstrap";
import ReactApexChart from "react-apexcharts";
import { Activity, AlertTriangle, ChevronRight, Clock, Radio } from "react-feather";
import { api } from "../api";
import { useConnected, useLive } from "../live";
import { ChatComposer, ChatThread, useChat } from "../chat";

// Severity is status, not identity: fixed meanings, always beside a written
// label and a count so the colour is never carrying it alone.
const THREAT = {
  critical: { color: "#f43f5e", label: "Critical" },
  high: { color: "#fb923c", label: "High" },
  medium: { color: "#facc15", label: "Medium" },
  low: { color: "#22c55e", label: "Low" },
  unknown: { color: "#64748b", label: "Unknown" },
};
const THREAT_ORDER = ["critical", "high", "medium", "low", "unknown"];

const SERIES = "#38bdf8";
const GRID = "rgba(148,163,184,.16)";
const AXIS = "#7d8b9a";
const REFRESH_MS = 30000;

function useOverview() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const pending = useRef(null);

  const load = useCallback(
    () => api.overview().then(setData).catch((e) => setError(e.message)),
    []
  );

  // A burst of events would otherwise mean a burst of identical queries.
  const nudge = useCallback(() => {
    if (pending.current) return;
    pending.current = setTimeout(() => {
      pending.current = null;
      load();
    }, 1000);
  }, [load]);

  useEffect(() => {
    load();
    // Uptime and the rolling window move on their own, with or without traffic.
    const t = setInterval(load, REFRESH_MS);
    return () => {
      clearInterval(t);
      if (pending.current) clearTimeout(pending.current);
    };
  }, [load]);

  useLive("event", nudge);
  useLive("escalation", nudge);
  useLive("sensor_state", nudge);
  useLive("alarm_state", nudge);

  return { data, error };
}

function humanUptime(seconds) {
  if (seconds == null) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

// --- tiles -----------------------------------------------------------------

function StatTile({ icon: Icon, label, value, sub, tone = "primary", spark, to }) {
  const body = (
    <Card className="h-100 mb-0">
      <CardBody className="d-flex align-items-start justify-content-between">
        <div>
          <div
            className="text-muted text-uppercase text-nowrap"
            style={{ fontSize: 11, letterSpacing: ".08em" }}
          >
            {label}
          </div>
          {/* Hero figure: proportional digits, same sans as everything else. */}
          <div className={`fw-bold text-${tone}`} style={{ fontSize: 34, lineHeight: 1.15 }}>
            {value}
          </div>
          {sub && <div className="text-muted small">{sub}</div>}
        </div>
        <div className="d-flex flex-column align-items-end gap-2">
          <span className={`text-${tone}`} style={{ opacity: 0.8 }}>
            <Icon size={20} />
          </span>
          {spark}
        </div>
      </CardBody>
    </Card>
  );
  return to ? (
    <Link to={to} className="text-decoration-none d-block h-100">
      {body}
    </Link>
  ) : (
    body
  );
}

function Sparkline({ points }) {
  if (!points?.length) return null;
  return (
    <ReactApexChart
      type="area"
      width={110}
      height={44}
      series={[{ name: "events", data: points.map((p) => p.count) }]}
      options={{
        chart: { sparkline: { enabled: true }, animations: { enabled: false } },
        stroke: { curve: "smooth", width: 2 },
        fill: { opacity: 0.1 },
        colors: [SERIES],
        tooltip: { enabled: false },
      }}
    />
  );
}

// --- panels ----------------------------------------------------------------

function EventHistogram({ points }) {
  const labels = points.map((p) => p.hour);
  return (
    <Card className="h-100 mb-0">
      <CardHeader className="border-0 pb-0">
        <h6 className="mb-0">Events</h6>
        <small className="text-muted">Last 24 hours, by hour</small>
      </CardHeader>
      <CardBody className="pt-2">
        <ReactApexChart
          type="bar"
          height={200}
          series={[{ name: "Events", data: points.map((p) => p.count) }]}
          options={{
            chart: { toolbar: { show: false }, fontFamily: "inherit", animations: { enabled: false } },
            colors: [SERIES],
            // Capped and rounded at the data end; the leftover band is air.
            plotOptions: { bar: { columnWidth: "55%", maxWidth: 24, borderRadius: 4, borderRadiusApplication: "end" } },
            dataLabels: { enabled: false },
            grid: { borderColor: GRID, strokeDashArray: 0, padding: { left: 4, right: 4 } },
            xaxis: {
              categories: labels,
              axisBorder: { color: GRID },
              axisTicks: { show: false },
              labels: {
                style: { colors: AXIS, fontSize: "11px" },
                rotate: 0,
                hideOverlappingLabels: true,
                // 24 hour stamps will not fit flat; every fourth reads cleanly.
                formatter: (v, _t, opts) =>
                  opts?.i === undefined || opts.i % 4 === 0 ? String(v).slice(11, 16) : "",
              },
            },
            yaxis: {
              labels: { style: { colors: AXIS, fontSize: "11px" }, formatter: (v) => Math.round(v) },
              min: 0,
            },
            tooltip: {
              theme: "dark",
              x: { formatter: (_v, { dataPointIndex }) => labels[dataPointIndex] },
            },
          }}
        />
      </CardBody>
    </Card>
  );
}

function ThreatBreakdown({ byThreat, open }) {
  const rows = THREAT_ORDER.filter((k) => byThreat?.[k]).map((k) => ({
    key: k, count: byThreat[k], ...THREAT[k],
  }));
  const max = Math.max(1, ...rows.map((r) => r.count));

  return (
    <Card className="h-100 mb-0">
      <CardHeader className="border-0 pb-0">
        <h6 className="mb-0">Open escalations</h6>
        <small className="text-muted">By threat level</small>
      </CardHeader>
      <CardBody className="pt-3">
        {rows.length === 0 ? (
          <div className="text-center py-4">
            <div className="text-success fw-bold" style={{ fontSize: 22 }}>All clear</div>
            <div className="text-muted small">Nothing is waiting on you.</div>
          </div>
        ) : (
          <>
            {rows.map((r) => (
              <div key={r.key} className="mb-3">
                <div className="d-flex justify-content-between small mb-1">
                  <span className="d-inline-flex align-items-center gap-2">
                    <span
                      className="rounded-circle"
                      style={{ width: 8, height: 8, background: r.color, display: "inline-block" }}
                    />
                    {r.label}
                  </span>
                  <span className="text-muted">{r.count}</span>
                </div>
                <div className="rounded" style={{ height: 6, background: GRID }}>
                  <div
                    className="rounded"
                    style={{ height: 6, width: `${(r.count / max) * 100}%`, background: r.color }}
                  />
                </div>
              </div>
            ))}
            <Link to="/escalations" className="small d-inline-flex align-items-center gap-1">
              Review all {open} <ChevronRight size={13} />
            </Link>
          </>
        )}
      </CardBody>
    </Card>
  );
}

function SensorRail() {
  const [rows, setRows] = useState([]);
  const load = useCallback(
    () => api.sensors({ limit: 50 }).then((r) => setRows(r.rows ?? [])).catch(() => {}),
    []
  );
  useEffect(() => { load(); }, [load]);
  useLive("sensor_state", load);

  const dot = (state) =>
    ({ online: "#22c55e", offline: "#f43f5e" }[state] ?? "#64748b");

  return (
    <Card className="h-100 mb-0">
      <CardHeader className="border-0 pb-0 d-flex justify-content-between align-items-center">
        <div>
          <h6 className="mb-0">Sensors</h6>
          <small className="text-muted">{rows.filter((r) => r.state === "online").length} of {rows.length} online</small>
        </div>
        <Link to="/sensors" className="small">All</Link>
      </CardHeader>
      <CardBody className="pt-2" style={{ maxHeight: 360, overflowY: "auto" }}>
        {rows.length === 0 && (
          <p className="text-muted small mb-0">No sensors yet. Install a plugin to get started.</p>
        )}
        <div className="list-group list-group-flush">
          {rows.map((s) => (
            <Link
              key={s.id}
              to={`/sensors/${encodeURIComponent(s.id)}`}
              className="list-group-item list-group-item-action bg-transparent d-flex align-items-center justify-content-between px-0"
            >
              <span className="d-inline-flex align-items-center gap-2 text-truncate">
                <span
                  className="rounded-circle flex-shrink-0"
                  style={{ width: 8, height: 8, background: dot(s.state), display: "inline-block" }}
                />
                <span className="text-truncate">
                  <span className="fw-semibold">{s.name}</span>
                  <span className="text-muted small ms-2">{s.plugin} · {s.kind}</span>
                </span>
              </span>
              <ChevronRight size={14} className="text-muted flex-shrink-0" />
            </Link>
          ))}
        </div>
      </CardBody>
    </Card>
  );
}

// Openers, not decoration: they are the fastest way to learn that the box
// answers questions about its own state, and each is a real tool call.
const OPENERS = [
  "What happened overnight?",
  "Which sensors are offline?",
  "Arm every alarm",
];

function ChatPanel({ assistant }) {
  const { turns, busy, send } = useChat();
  const connected = useConnected();

  return (
    <Card className="h-100 mb-0">
      <CardHeader className="border-0 pb-2">
        <h6 className="mb-0">{assistant}</h6>
        <small className="text-muted">
          {connected ? "Listening on this network" : "Reconnecting…"}
        </small>
      </CardHeader>
      <CardBody className="d-flex flex-column pt-0" style={{ height: 380 }}>
        <div className="flex-grow-1 overflow-auto mb-3 pe-1">
          {turns.length === 0 ? (
            <div className="h-100 d-flex flex-column justify-content-center text-center px-3">
              <p className="text-muted small mb-3">
                Ask about a sensor, search events, or arm the house.
                <br />
                Anything typed here works spoken too.
              </p>
              <div className="d-flex flex-wrap justify-content-center gap-2">
                {OPENERS.map((o) => (
                  <button
                    key={o}
                    type="button"
                    className="btn btn-outline-primary btn-sm rounded-pill"
                    disabled={busy}
                    onClick={() => send(o)}
                  >
                    {o}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <ChatThread turns={turns} busy={busy} />
          )}
        </div>
        <ChatComposer onSend={send} busy={busy} assistant={assistant} />
      </CardBody>
    </Card>
  );
}

function pluginTone(state) {
  if (["healthy", "running", "started"].includes(state)) return "#22c55e";
  if (["starting", "restarting", "degraded"].includes(state)) return "#facc15";
  if (state === "stopped") return "#64748b";
  return "#f43f5e";
}

function PluginHealth({ plugins }) {
  if (!plugins?.length) return null;
  return (
    <Card className="h-100 mb-0">
      <CardHeader className="border-0 pb-0">
        <h6 className="mb-0">Plugins</h6>
        <small className="text-muted">Supervisor state</small>
      </CardHeader>
      <CardBody className="pt-2">
        {plugins.map((p) => (
          <div key={p.plugin} className="d-flex justify-content-between align-items-center py-1 small">
            <span className="d-inline-flex align-items-center gap-2">
              <span
                className="rounded-circle"
                style={{ width: 8, height: 8, display: "inline-block", background: pluginTone(p.state) }}
              />
              {p.plugin}
              <span className="text-muted">v{p.version}</span>
            </span>
            <span className="text-muted">
              {p.state}
              {p.restarts > 0 && ` · ${p.restarts} restart${p.restarts === 1 ? "" : "s"}`}
            </span>
          </div>
        ))}
      </CardBody>
    </Card>
  );
}

// --- page ------------------------------------------------------------------

export default function Home() {
  const { assistant = "Ice" } = useOutletContext() ?? {};
  const { data, error } = useOverview();

  if (error) return <div className="alert alert-danger">{error}</div>;
  if (!data) return null;

  const { sensors, events, escalations, alarms, plugins, uptime_seconds, llm } = data;
  const offline = sensors.total - sensors.online;

  return (
    <>
      <Row className="g-3 mb-3">
        <Col xl={3} md={6}>
          <StatTile
            icon={Clock}
            label="Uptime"
            value={humanUptime(uptime_seconds)}
            sub={llm?.avg_latency_ms ? `${llm.avg_latency_ms}ms average reply` : "No model traffic yet"}
          />
        </Col>
        <Col xl={3} md={6}>
          <StatTile
            icon={Radio}
            label="Sensors online"
            value={`${sensors.online}/${sensors.total}`}
            sub={offline > 0 ? `${offline} not reporting` : "All reporting"}
            tone={offline > 0 ? "warning" : "success"}
            to="/sensors"
          />
        </Col>
        <Col xl={3} md={6}>
          <StatTile
            icon={Activity}
            label="Events today"
            value={events.last_24h.toLocaleString()}
            sub={`${events.total.toLocaleString()} all time`}
            spark={<Sparkline points={events.histogram} />}
            to="/events"
          />
        </Col>
        <Col xl={3} md={6}>
          <StatTile
            icon={AlertTriangle}
            label="Open escalations"
            value={escalations.open}
            sub={`${alarms.armed}/${alarms.total} alarms armed`}
            tone={escalations.open > 0 ? "danger" : "success"}
            to="/escalations"
          />
        </Col>
      </Row>

      <Row className="g-3 mb-3">
        <Col xl={8}>
          <ChatPanel assistant={assistant} />
        </Col>
        <Col xl={4}>
          <SensorRail />
        </Col>
      </Row>

      <Row className="g-3">
        <Col xl={6}>
          <EventHistogram points={events.histogram} />
        </Col>
        <Col xl={3} md={6}>
          <ThreatBreakdown byThreat={escalations.by_threat} open={escalations.open} />
        </Col>
        <Col xl={3} md={6}>
          <PluginHealth plugins={plugins} />
        </Col>
      </Row>
    </>
  );
}
