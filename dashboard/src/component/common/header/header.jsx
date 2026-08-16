// The header carries the four things an operator wants without clicking:
// whether the feed is live, whether the house is armed, what is happening
// (search), and who is signed in. Everything the template shipped here --
// the demo notifications, the app-grid droplet, the bookmark bar, the fake
// profile -- pointed at routes that do not exist and is gone.

import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { AlignCenter, LogOut, Maximize, Moon, Search, Sun, User } from "react-feather";
import { Badge, Input, Spinner } from "reactstrap";
import { Link, useNavigate } from "react-router-dom";
import { useDispatch, useSelector } from "react-redux";
import { ADD_COLOR } from "../../../redux/customizer/CustomizerSlice";
import Logo from "../../../blackice/Logo";
import { api } from "../../../blackice/api";
import { useConnected, useLive } from "../../../blackice/live";

const SEARCH_DEBOUNCE_MS = 250;

function useWindowWidth() {
  const [width, setWidth] = useState(window.innerWidth);
  useLayoutEffect(() => {
    const onResize = () => setWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return width;
}

// --- pieces ----------------------------------------------------------------

function LivePill() {
  const connected = useConnected();
  return (
    <span
      className={`badge d-inline-flex align-items-center gap-1 ${
        connected ? "bg-success" : "bg-danger"
      }`}
      title={connected ? "Receiving live updates" : "Disconnected -- reconnecting"}
    >
      <span
        className="rounded-circle bg-white"
        style={{ width: 6, height: 6, opacity: connected ? 1 : 0.6 }}
      />
      {connected ? "LIVE" : "OFFLINE"}
    </span>
  );
}

// The master arm switch. Reads real alarm state rather than a local toggle,
// so a rule armed by voice shows up here too.
function ArmSwitch() {
  const [rules, setRules] = useState([]);
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    () => api.alarms().then((r) => setRules(r.rows ?? r ?? [])).catch(() => {}),
    []
  );
  useEffect(() => { load(); }, [load]);
  useLive("alarm_state", load);

  const armed = rules.filter((r) => r.armed).length;
  const all = rules.length;
  if (!all) return null;

  const toggle = async () => {
    setBusy(true);
    try {
      await api.setAllAlarms(armed < all);
      await load();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="d-flex align-items-center gap-2">
      <div className="form-check form-switch mb-0">
        <input
          className="form-check-input"
          type="checkbox"
          role="switch"
          id="master-arm"
          checked={armed === all}
          disabled={busy}
          onChange={toggle}
          aria-label={armed === all ? "Disarm every alarm" : "Arm every alarm"}
        />
      </div>
      <label htmlFor="master-arm" className="mb-0 small text-nowrap" style={{ cursor: "pointer" }}>
        {armed === 0 ? (
          <span className="text-muted">Disarmed</span>
        ) : (
          <span className={armed === all ? "text-success" : "text-warning"}>
            Armed {armed}/{all}
          </span>
        )}
      </label>
    </div>
  );
}

function HeaderSearch() {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState(null);
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const box = useRef(null);

  useEffect(() => {
    const term = q.trim();
    if (!term) {
      setHits(null);
      return undefined;
    }
    let cancelled = false;
    setBusy(true);
    const t = setTimeout(() => {
      Promise.all([
        api.sensors({ q: term, limit: 5 }).catch(() => ({ rows: [] })),
        api.events({ q: term, limit: 5 }).catch(() => ({ rows: [] })),
      ])
        .then(([s, e]) => {
          if (cancelled) return;
          setHits({ sensors: s.rows ?? [], events: e.rows ?? [] });
        })
        .finally(() => !cancelled && setBusy(false));
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [q]);

  // Click-away and Escape both close the results.
  useEffect(() => {
    const away = (e) => !box.current?.contains(e.target) && setHits(null);
    const esc = (e) => e.key === "Escape" && (setQ(""), setHits(null));
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, []);

  const go = (path) => {
    setQ("");
    setHits(null);
    navigate(path);
  };

  const empty = hits && !hits.sensors.length && !hits.events.length;

  return (
    <div className="position-relative" ref={box} style={{ width: 280, maxWidth: "40vw" }}>
      <Search size={15} className="position-absolute top-50 translate-middle-y ms-2 text-muted" />
      <Input
        bsSize="sm"
        className="ps-5 pe-4"
        value={q}
        placeholder="Search sensors and events…"
        aria-label="Search sensors and events"
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && q.trim() && go(`/events?q=${encodeURIComponent(q.trim())}`)}
      />
      {busy && (
        <Spinner size="sm" className="position-absolute top-50 translate-middle-y text-muted" style={{ right: 10 }} />
      )}
      {hits && (
        <div
          className="position-absolute bg-body border rounded shadow-lg mt-1 w-100 overflow-auto"
          style={{ zIndex: 1050, maxHeight: 340 }}
        >
          {empty && <div className="small text-muted px-3 py-3">Nothing matches “{q}”.</div>}
          {hits.sensors.length > 0 && <SearchGroup label="Sensors" />}
          {hits.sensors.map((s) => (
            <button
              key={s.id}
              type="button"
              className="btn btn-link text-decoration-none d-block w-100 text-start px-3 py-2 small text-body"
              onClick={() => go(`/sensors/${encodeURIComponent(s.id)}`)}
            >
              <span className="d-flex justify-content-between align-items-center gap-2">
                <span className="text-truncate">{s.name || s.id}</span>
                <Badge color={s.state === "online" ? "success" : "secondary"} pill>
                  {s.state}
                </Badge>
              </span>
            </button>
          ))}
          {hits.events.length > 0 && <SearchGroup label="Events" />}
          {hits.events.map((e) => (
            <button
              key={e.id}
              type="button"
              className="btn btn-link text-decoration-none d-block w-100 text-start px-3 py-2 small text-body"
              onClick={() => go(`/events?q=${encodeURIComponent(q.trim())}`)}
            >
              <span className="d-block text-truncate">{e.summary || e.kind}</span>
              <span className="text-muted" style={{ fontSize: 11 }}>{e.ts}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function SearchGroup({ label }) {
  return (
    <div className="px-3 pt-2 pb-1 text-uppercase text-muted" style={{ fontSize: 10, letterSpacing: ".1em" }}>
      {label}
    </div>
  );
}

// The one survivor of the template's customizer panel: everything else it
// offered (six palettes, box layouts, RTL) is not a choice this product makes.
function ThemeToggle() {
  const dispatch = useDispatch();
  const color = useSelector((s) => s.customizerSlice.customizer.color);
  const dark = color.layout_version !== "light";

  const flip = () => {
    const next = dark ? "light" : "dark-only";
    localStorage.setItem("layout_version", next);
    document.body.className = next;
    dispatch(ADD_COLOR({ ...color, layout_version: next }));
  };

  return (
    <button
      type="button"
      className="btn btn-link p-1 text-body"
      onClick={flip}
      title={dark ? "Switch to light" : "Switch to dark"}
      aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
    >
      {dark ? <Sun size={18} /> : <Moon size={18} />}
    </button>
  );
}

function goFullscreen() {
  const doc = document;
  if (doc.fullscreenElement) {
    doc.exitFullscreen?.();
  } else {
    doc.documentElement.requestFullscreen?.();
  }
}

// --- header ----------------------------------------------------------------

const Header = ({ username = "admin", assistant = "Ice" }) => {
  const [sidebar, setSidebar] = useState("iconsidebar-menu");
  const [menuOpen, setMenuOpen] = useState(false);
  const width = useWindowWidth();
  const account = useRef(null);

  useEffect(() => {
    if (!menuOpen) return undefined;
    const away = (e) => !account.current?.contains(e.target) && setMenuOpen(false);
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [menuOpen]);

  useEffect(() => {
    const el = document.querySelector(".iconsidebar-menu");
    if (!el) return;
    if (width <= 991) {
      setSidebar("iconbar-second-close");
      el.classList.add("iconbar-second-close");
    } else {
      setSidebar("iconsidebar-menu");
      el.classList.remove("iconbar-second-close");
    }
  }, [width]);

  const toggleSidebar = () => {
    const el = document.querySelector(".iconsidebar-menu");
    if (!el) return;
    const anyOpen = [...(document.querySelector(".iconMenu-bar")?.children ?? [])].some((li) =>
      li.classList.contains("open")
    );
    if (sidebar === "iconsidebar-menu") {
      setSidebar("iconbar-second-close");
      el.classList.remove("iconbar-mainmenu-close");
      el.classList.add("iconbar-second-close");
    } else {
      setSidebar("iconsidebar-menu");
      el.classList.remove("iconbar-second-close");
      if (!anyOpen) el.classList.add("iconbar-mainmenu-close");
    }
  };

  const logout = async () => {
    try {
      await api.logout();
    } finally {
      // Full reload rather than a route change: it drops every cached panel
      // and sends the session check back through the login screen.
      window.location.replace("/");
    }
  };

  return (
    <div className="page-main-header">
      <div className="main-header-right">
        <div className="main-header-left text-center">
          <div className="logo-wrapper">
            <Link to="/" aria-label="Black Ice home">
              <Logo size={26} />
            </Link>
          </div>
        </div>

        <div className="mobile-sidebar">
          <div className="media-body text-end switch-sm">
            <label className="switch ms-3">
              <AlignCenter className="font-primary" onClick={toggleSidebar} />
            </label>
          </div>
        </div>

        <div className="nav-right col pull-right right-menu">
          <div className="d-flex align-items-center justify-content-end gap-3 flex-wrap py-2 pe-2">
            <HeaderSearch />
            <LivePill />
            <ArmSwitch />
            <ThemeToggle />
            <button
              type="button"
              className="btn btn-link p-1 text-body d-none d-md-inline-block"
              onClick={goFullscreen}
              title="Fullscreen"
              aria-label="Toggle fullscreen"
            >
              <Maximize size={18} />
            </button>

            <div className="position-relative" ref={account}>
              <button
                type="button"
                className="btn btn-link p-1 text-body d-flex align-items-center gap-2"
                onClick={() => setMenuOpen((o) => !o)}
                aria-expanded={menuOpen}
                aria-label="Account menu"
              >
                <span
                  className="rounded-circle bg-primary text-white d-inline-flex align-items-center justify-content-center"
                  style={{ width: 30, height: 30, fontSize: 13 }}
                >
                  {username.slice(0, 1).toUpperCase()}
                </span>
                <span className="small d-none d-lg-inline">{username}</span>
              </button>
              {menuOpen && (
                <div
                  className="position-absolute end-0 mt-1 bg-body border rounded shadow-lg py-1"
                  style={{ zIndex: 1050, minWidth: 190 }}
                >
                  <div className="px-3 py-2 border-bottom">
                    <div className="small fw-bold d-flex align-items-center gap-2">
                      <User size={14} /> {username}
                    </div>
                    <div className="text-muted" style={{ fontSize: 11 }}>
                      Talking to {assistant}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="btn btn-link text-decoration-none d-block w-100 text-start px-3 py-2 small text-body"
                    onClick={logout}
                  >
                    <LogOut size={14} className="me-2" />
                    Sign out
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default Header;
