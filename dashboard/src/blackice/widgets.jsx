// Renderer registry for plugin-declared WidgetSpecs. Plugins ship JSON, never
// browser code; this maps each spec type onto a Poco component.
//
// Adding a widget type is one entry here plus one component.

import React, { useEffect, useState } from "react";
import { Badge, Card, CardBody, CardHeader, Col, Spinner, Table } from "reactstrap";
import ReactApexChart from "react-apexcharts";
import { AlertTriangle } from "react-feather";
import { api } from "./api";

const CHART_BASE = {
  chart: { toolbar: { show: false }, fontFamily: "inherit" },
  dataLabels: { enabled: false },
  grid: { borderColor: "rgba(128,128,128,.15)" },
  tooltip: { theme: "dark" },
};

// --- individual renderers --------------------------------------------------

function Stat({ data, props }) {
  return (
    <div className="text-center py-2">
      <h2 className="mb-0">{data?.value ?? props?.value ?? "—"}</h2>
      <small className="text-muted">{data?.label ?? props?.label ?? ""}</small>
    </div>
  );
}

function Gauge({ data, props }) {
  const value = Number(data?.value ?? props?.value ?? 0);
  return (
    <ReactApexChart
      type="radialBar"
      height={200}
      series={[Math.max(0, Math.min(100, value))]}
      options={{ ...CHART_BASE, labels: [data?.label ?? props?.label ?? ""] }}
    />
  );
}

function toSeries(rows) {
  if (!Array.isArray(rows)) return { categories: [], values: [] };
  const keys = rows.length ? Object.keys(rows[0]) : [];
  const xKey = keys[0];
  const yKey = keys[1] ?? keys[0];
  return {
    categories: rows.map((r) => String(r[xKey])).reverse(),
    values: rows.map((r) => Number(r[yKey]) || 0).reverse(),
  };
}

function TimeSeries({ data, props, type = "area" }) {
  const { categories, values } = toSeries(data);
  return (
    <ReactApexChart
      type={type}
      height={props?.height ?? 240}
      series={[{ name: props?.seriesName ?? "value", data: values }]}
      options={{ ...CHART_BASE, xaxis: { categories }, stroke: { curve: "smooth", width: 2 } }}
    />
  );
}

function Donut({ data, props }) {
  const { categories, values } = toSeries(data);
  return (
    <ReactApexChart
      type="donut"
      height={props?.height ?? 240}
      series={values}
      options={{ ...CHART_BASE, labels: categories }}
    />
  );
}

function DataTable({ data }) {
  const rows = Array.isArray(data) ? data : [];
  if (!rows.length) return <Empty />;
  const cols = Object.keys(rows[0]);
  return (
    <div className="table-responsive">
      <Table className="table-sm mb-0">
        <thead>
          <tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>{cols.map((c) => <td key={c}>{format(r[c])}</td>)}</tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}

function KeyValue({ data, props }) {
  const obj = data ?? props ?? {};
  const entries = Object.entries(obj);
  if (!entries.length) return <Empty />;
  return (
    <dl className="row mb-0">
      {entries.map(([k, v]) => (
        <React.Fragment key={k}>
          <dt className="col-5 text-muted fw-normal small">{k}</dt>
          <dd className="col-7 small mb-1">{format(v)}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}

function LogList({ data }) {
  const rows = Array.isArray(data) ? data : [];
  if (!rows.length) return <Empty />;
  return (
    <div style={{ maxHeight: 300, overflowY: "auto" }}>
      <ul className="list-unstyled mb-0 small font-monospace">
        {rows.map((r, i) => (
          <li key={i} className="border-bottom py-1">
            {Object.values(r).map(format).join("  ·  ")}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Status({ data, props }) {
  const state = data?.state ?? props?.state ?? "unknown";
  const tone = { online: "success", healthy: "success", offline: "danger",
                 unhealthy: "danger", degraded: "warning" }[state] ?? "secondary";
  return <Badge color={tone} className="text-uppercase">{state}</Badge>;
}

function ImageWidget({ data, props }) {
  const src = data?.url ?? props?.url;
  if (!src) return <Empty />;
  return <img src={src} alt={props?.alt ?? "sensor capture"} className="img-fluid rounded" />;
}

function Gallery({ data }) {
  const items = Array.isArray(data) ? data : [];
  if (!items.length) return <Empty />;
  return (
    <div className="d-flex flex-wrap gap-2">
      {items.map((it, i) => (
        <a key={i} href={it.url} target="_blank" rel="noreferrer">
          <img src={it.thumb ?? it.url} alt="" style={{ height: 90 }} className="rounded" />
        </a>
      ))}
    </div>
  );
}

function VideoWidget({ data, props }) {
  const stream = data ?? props ?? {};
  if (!stream.url) return <Empty message="No stream provided" />;
  if (stream.kind === "mjpeg") {
    return <img src={stream.url} alt="live view" className="img-fluid rounded w-100" />;
  }
  return (
    <video className="w-100 rounded" controls autoPlay muted playsInline src={stream.url}>
      <track kind="captions" />
    </video>
  );
}

function AudioWidget({ data, props }) {
  const src = data?.url ?? props?.url;
  if (!src) return <Empty />;
  return <audio className="w-100" controls src={src}><track kind="captions" /></audio>;
}

function MapWidget({ data, props }) {
  const { lat, lon, label } = { ...props, ...data };
  if (lat == null || lon == null) return <Empty />;
  return (
    <div className="small">
      <strong>{label ?? "Location"}</strong>
      <div className="text-muted">{lat}, {lon}</div>
    </div>
  );
}

function Toggle({ data, props, onToggle }) {
  const armed = Boolean(data?.armed ?? props?.armed);
  return (
    <div className="form-check form-switch">
      <input
        className="form-check-input"
        type="checkbox"
        role="switch"
        checked={armed}
        onChange={(e) => onToggle?.(e.target.checked)}
      />
      <label className="form-check-label">{armed ? "Armed" : "Disarmed"}</label>
    </div>
  );
}

// --- registry --------------------------------------------------------------

export const WIDGETS = {
  stat: Stat,
  gauge: Gauge,
  timeseries: TimeSeries,
  bar: (p) => <TimeSeries {...p} type="bar" />,
  donut: Donut,
  table: DataTable,
  kv: KeyValue,
  log: LogList,
  status: Status,
  image: ImageWidget,
  gallery: Gallery,
  video: VideoWidget,
  audio: AudioWidget,
  map: MapWidget,
  toggle: Toggle,
};

function Empty({ message = "No data yet" }) {
  return <div className="text-muted small py-3 text-center">{message}</div>;
}

function Unknown({ spec }) {
  // Never render a blank panel: an unknown type is a plugin/dashboard version
  // mismatch, and saying so beats showing nothing.
  return (
    <div className="text-muted small py-3 text-center">
      <AlertTriangle size={16} className="me-1" />
      No renderer for widget type <code>{spec.type}</code>
    </div>
  );
}

export function Widget({ sensorId, spec, onToggle }) {
  const Renderer = WIDGETS[spec.type];
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(Boolean(spec.data_source));
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!spec.data_source || !sensorId) return undefined;
    let cancelled = false;
    api
      .widgetData(sensorId, spec.data_source)
      .then((r) => !cancelled && setData(r.data))
      .catch((e) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [sensorId, spec.data_source]);

  return (
    <Col md={spec.span ?? 6} className="mb-3">
      <Card className="h-100 mb-0">
        {spec.title && <CardHeader className="py-2"><h6 className="mb-0">{spec.title}</h6></CardHeader>}
        <CardBody>
          {loading ? (
            <div className="text-center py-3"><Spinner size="sm" /></div>
          ) : error ? (
            <div className="text-danger small">{error}</div>
          ) : Renderer ? (
            <Renderer data={data} props={spec.props} onToggle={onToggle} />
          ) : (
            <Unknown spec={spec} />
          )}
        </CardBody>
      </Card>
    </Col>
  );
}

function format(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
