// Renderer registry for plugin-declared WidgetSpecs. Plugins ship JSON, never
// browser code; this maps each spec type onto a Poco component.
//
// Adding a widget type is one entry here plus one component.

import React, { useCallback, useEffect, useState } from "react";
import { Button, Card, CardBody, CardHeader, Col, Input, Spinner, Table } from "reactstrap";
import ReactApexChart from "react-apexcharts";
import { AlertTriangle } from "react-feather";
import { api } from "./api";
import { Pill } from "./ui";
import { CHART, SENSOR_STATE } from "./tokens";

const CHART_BASE = {
  chart: { toolbar: { show: false }, fontFamily: "inherit" },
  colors: [CHART.series],
  dataLabels: { enabled: false },
  grid: { borderColor: CHART.grid },
  xaxis: { labels: { style: { colors: CHART.axis, fontSize: "11px" } } },
  yaxis: { labels: { style: { colors: CHART.axis, fontSize: "11px" } } },
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
  const color = { online: "#22c55e", healthy: "#22c55e", offline: SENSOR_STATE.offline,
                  unhealthy: SENSOR_STATE.offline, degraded: "#facc15" }[state]
                ?? SENSOR_STATE.unknown;
  return <Pill color={color}>{String(state).toUpperCase()}</Pill>;
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

// Positions plotted against each other, not against the world: there is no
// tile layer here and there deliberately never will be, because fetching one
// would tell a map server where the household is. A `points` array gets a
// scatter with labels; a bare lat/lon still renders as it always did.
function MapWidget({ data, props }) {
  const merged = { ...props, ...data };
  const { lat, lon, label } = merged;
  const points = (Array.isArray(merged.points) ? merged.points : [])
    .filter((p) => p && p.lat != null && p.lon != null);

  if (!points.length) {
    if (lat == null || lon == null) return <Empty />;
    return (
      <div className="small">
        <strong>{label ?? "Location"}</strong>
        <div className="text-muted">{lat}, {lon}</div>
      </div>
    );
  }

  const lats = points.map((p) => Number(p.lat));
  const lons = points.map((p) => Number(p.lon));
  const bounds = {
    minLat: Math.min(...lats), maxLat: Math.max(...lats),
    minLon: Math.min(...lons), maxLon: Math.max(...lons),
  };
  // A single point — or several at one address — has no extent to scale to.
  const spanLat = bounds.maxLat - bounds.minLat || 1;
  const spanLon = bounds.maxLon - bounds.minLon || 1;
  const place = (p) => ({
    // 8% padding so a pin on the edge is not half outside the box.
    left: `${8 + ((Number(p.lon) - bounds.minLon) / spanLon) * 84}%`,
    top: `${8 + (1 - (Number(p.lat) - bounds.minLat) / spanLat) * 84}%`,
  });

  return (
    <div
      className="position-relative rounded"
      style={{ height: 260, background: CHART.grid, overflow: "hidden" }}
    >
      {points.map((p, i) => (
        <div
          key={`${p.label ?? i}-${p.lat},${p.lon}`}
          className="position-absolute d-flex align-items-center gap-1"
          style={{ ...place(p), transform: "translate(-50%, -50%)" }}
        >
          <span
            className="rounded-circle flex-shrink-0"
            style={{
              width: 10, height: 10, display: "inline-block",
              background: p.label === "Home" ? SENSOR_STATE.unknown : CHART.series,
            }}
          />
          <small className="text-nowrap" style={{ color: CHART.axis }}>{p.label}</small>
        </div>
      ))}
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

// A button that runs one of the plugin's own commands. The dashboard still
// ships no plugin-specific code: the data source says what the button is
// called, which command it runs, what to warn about first, and — through
// `fields` — what to ask for before running it. A field's `name` is the
// command's argument name, so a form is declared in JSON like everything else.
function Action({ data, props, sensorId, refresh }) {
  const spec = { ...props, ...data };
  const fields = Array.isArray(spec.fields) ? spec.fields : [];
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState(null);
  const [error, setError] = useState(null);
  const [values, setValues] = useState({});
  const running = busy || Boolean(spec.busy);

  // Refetching the data source rebuilds `fields`, so seed from it only when the
  // shape actually changes — otherwise every poll would wipe what was typed.
  const shape = JSON.stringify(fields.map((f) => [f.name, f.type, f.options]));
  useEffect(() => {
    if (!fields.length) return;
    setValues(
      Object.fromEntries(
        fields.map((f) => [f.name, f.value ?? firstOption(f) ?? ""]),
      ),
    );
  }, [shape]); // eslint-disable-line react-hooks/exhaustive-deps

  // While the command is still working, keep asking: a long job (a model
  // download, say) reports progress through its own data source.
  useEffect(() => {
    if (!spec.busy || !refresh) return undefined;
    const timer = setInterval(refresh, 3000);
    return () => clearInterval(timer);
  }, [spec.busy, refresh]);

  async function run() {
    const missing = fields.find((f) => f.required && !String(values[f.name] ?? "").trim());
    if (missing) {
      setError(`${missing.label ?? missing.name} is required.`);
      return;
    }
    if (spec.confirm && !window.confirm(spec.confirm)) return;
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const args = { ...spec.arguments, ...values };
      const { result } = await api.runAction(sensorId, spec.command, args);
      if (result?.error) setError(result.error);
      else setNote(result?.note ?? "Done.");
      refresh?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const color = { ready: "#22c55e", busy: "#facc15", missing: SENSOR_STATE.unknown,
                  blocked: SENSOR_STATE.offline }[spec.state] ?? SENSOR_STATE.unknown;

  return (
    <div>
      {fields.map((f) => (
        <div className="mb-2" key={f.name}>
          {f.label && <label className="form-label small mb-1">{f.label}</label>}
          <Input
            type={f.type === "select" ? "select" : f.type ?? "text"}
            bsSize="sm"
            value={values[f.name] ?? ""}
            placeholder={f.placeholder ?? ""}
            min={f.min}
            max={f.max}
            step={f.step}
            disabled={running}
            onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
          >
            {f.type === "select" &&
              (f.options ?? []).map((o) => {
                const value = o?.value ?? o;
                return (
                  <option key={value} value={value}>
                    {o?.label ?? value}
                  </option>
                );
              })}
          </Input>
        </div>
      ))}
      <div className="d-flex align-items-center gap-2 mb-2">
        {spec.state && <Pill color={color}>{String(spec.state).toUpperCase()}</Pill>}
        <Button size="sm" color="primary" disabled={running || !spec.command} onClick={run}>
          {running ? <Spinner size="sm" /> : (spec.label ?? "Run")}
        </Button>
      </div>
      {spec.detail && <div className="text-muted small">{spec.detail}</div>}
      {note && <div className="text-success small mt-1">{note}</div>}
      {error && <div className="text-danger small mt-1">{error}</div>}
    </div>
  );
}

// An empty-string value is a legitimate default (the "all addresses" option),
// so fall back on the option's presence rather than its truthiness.
function firstOption(field) {
  if (field.type !== "select") return undefined;
  const first = (field.options ?? [])[0];
  if (first === undefined) return undefined;
  return first?.value ?? first;
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
  action: Action,
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

  // Held in a callback rather than inlined in the effect so that an `action`
  // widget can ask for fresh data after running its command.
  const load = useCallback(() => {
    if (!spec.data_source || !sensorId) return Promise.resolve();
    return api
      .widgetData(sensorId, spec.data_source)
      .then((r) => setData(r.data))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [sensorId, spec.data_source]);

  useEffect(() => {
    load();
  }, [load]);

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
            <Renderer data={data} props={spec.props} onToggle={onToggle}
                      sensorId={sensorId} refresh={load} />
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
