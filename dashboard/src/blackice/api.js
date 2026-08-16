// Single place that talks to the backend. CSRF token rides on every unsafe
// request, matching blackice/api/auth.py.

const CSRF_COOKIE = "blackice_csrf";

function csrf() {
  const hit = document.cookie.split("; ").find((c) => c.startsWith(`${CSRF_COOKIE}=`));
  return hit ? decodeURIComponent(hit.split("=")[1]) : "";
}

async function request(method, path, body) {
  const opts = {
    method,
    credentials: "same-origin",
    headers: { "x-csrf-token": csrf() },
  };
  if (body !== undefined) {
    opts.headers["content-type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`/api${path}`, opts);
  if (res.status === 401) {
    window.location.hash = "#/login";
    throw new Error("Not authenticated");
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new Error(data?.detail || res.statusText);
  return data;
}

function query(params = {}) {
  const q = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")
  ).toString();
  return q ? `?${q}` : "";
}

export const api = {
  get: (p, params) => request("GET", `${p}${query(params)}`),
  post: (p, body) => request("POST", p, body ?? {}),
  patch: (p, body) => request("PATCH", p, body ?? {}),
  del: (p) => request("DELETE", p),

  health: () => api.get("/health"),
  plugins: () => api.get("/plugins"),

  sensors: (params) => api.get("/sensors", params),
  sensor: (id) => api.get(`/sensors/${encodeURIComponent(id)}`),
  widgetData: (id, source) =>
    api.get(`/sensors/${encodeURIComponent(id)}/widgets/${encodeURIComponent(source)}`),

  groups: () => api.get("/groups"),
  createGroup: (name) => api.post("/groups", { name }),
  updateGroup: (id, patch) => api.patch(`/groups/${id}`, patch),
  deleteGroup: (id) => api.del(`/groups/${id}`),
  addToGroup: (gid, sid) => api.post(`/groups/${gid}/sensors/${encodeURIComponent(sid)}`),
  removeFromGroup: (gid, sid) => api.del(`/groups/${gid}/sensors/${encodeURIComponent(sid)}`),

  events: (params) => api.get("/events", params),
  event: (id) => api.get(`/events/${id}`),

  escalations: (params) => api.get("/escalations", params),
  escalation: (id) => api.get(`/escalations/${id}`),
  setEscalationStatus: (id, status) => api.patch(`/escalations/${id}`, { status }),
  addVerdict: (id, verdict, note) => api.post(`/escalations/${id}/verdict`, { verdict, note }),

  alarms: (params) => api.get("/alarms", params),
  setAlarmArmed: (id, armed) => api.post(`/alarms/${id}/armed`, { armed }),
  setAllAlarms: (armed) => api.post("/alarms/all", { armed }),

  chat: (message, sessionId = "console") => api.post("/chat", { message, session_id: sessionId }),

  login: async (username, password) => {
    const form = new FormData();
    form.append("username", username);
    form.append("password", password);
    const res = await fetch("/api/login", {
      method: "POST",
      body: form,
      credentials: "same-origin",
    });
    if (!res.ok) throw new Error("Invalid credentials");
    return res.json();
  },
  logout: () => api.post("/logout"),
};
