"""The REST surface, and the equivalence between clicking and speaking."""

import pytest
from fastapi.testclient import TestClient

from blackice import db
from blackice.api import auth
from blackice.api.app import create_app
from blackice.llm import guard
from blackice.llm.coretools import register_core_tools
from blackice.llm.tools import ToolRegistry
from blackice.models import Event
from blackice.services import events


@pytest.fixture
async def client(data_dir, monkeypatch):
    monkeypatch.setattr(guard.guard_model, "score", lambda t: 0.0)
    app = create_app()
    # The app is already wired by the data_dir fixture's connection; skip the
    # lifespan so plugin discovery does not leak between tests.
    app.router.lifespan_context = _noop_lifespan
    c = TestClient(app)
    token, csrf = auth.issue_session("admin")
    c.cookies.set(auth.SESSION_COOKIE, token)
    c.headers[auth.CSRF_HEADER] = csrf
    with c:
        yield c


def _noop_lifespan(app):
    class _Ctx:
        async def __aenter__(self): return None
        async def __aexit__(self, *a): return False
    return _Ctx()


@pytest.fixture
async def seeded(data_dir):
    for sid, name in [("cam.front", "Front Door"), ("cam.rear", "Back Gate")]:
        await db.execute(
            "INSERT INTO sensors (id, plugin, name) VALUES (?, 'rtsp', ?)", (sid, name)
        )
    rule_id = await db.execute(
        "INSERT INTO alarm_rules (plugin, key, name, sensor_id)"
        " VALUES ('rtsp','front_motion','Front door motion','cam.front')"
    )
    await db.execute("INSERT INTO alarm_state (rule_id, armed) VALUES (?, 0)", (rule_id,))
    eid = await events.record(Event(
        sensor_id="cam.front", plugin="rtsp", severity=3,
        kind="person", summary="Unknown person at front door",
    ))
    esc_id = await db.execute(
        """INSERT INTO escalations (event_id, threat_level, classification,
                                    reasoning, suggested_action)
           VALUES (?, 'high', 'Unknown person', 'Not recognised.', 'Check the camera.')""",
        (eid,),
    )
    return {"event_id": eid, "escalation_id": esc_id, "rule_id": rule_id}


# --- auth ------------------------------------------------------------------

def test_all_api_routes_require_auth(data_dir):
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as anon:
        for path in ["/api/sensors", "/api/events", "/api/escalations", "/api/alarms"]:
            assert anon.get(path).status_code == 401, path


# --- lists: search and date range -----------------------------------------

def test_sensor_list_and_search(client, seeded):
    assert client.get("/api/sensors").json()["total"] == 2
    assert client.get("/api/sensors", params={"q": "Back"}).json()["total"] == 1


def test_event_list_search_and_range(client, seeded):
    assert client.get("/api/events", params={"q": "person"}).json()["total"] == 1
    assert client.get("/api/events", params={"q": "giraffe"}).json()["total"] == 0
    assert client.get(
        "/api/events", params={"start": "2000-01-01", "end": "2000-01-02"}
    ).json()["total"] == 0


def test_escalation_detail_carries_everything_the_ui_shows(client, seeded):
    r = client.get(f"/api/escalations/{seeded['escalation_id']}").json()
    assert r["classification"] == "Unknown person"
    assert r["reasoning"] and r["suggested_action"]
    assert r["sensor"]["name"] == "Front Door"          # reporting sensor
    assert r["event"]["summary"] == "Unknown person at front door"  # event details
    assert r["event"]["media"] == []


def test_unknown_ids_are_404_not_500(client, seeded):
    assert client.get("/api/sensors/nope").status_code == 404
    assert client.get("/api/events/9999").status_code == 404
    assert client.get("/api/escalations/9999").status_code == 404


# --- groups ----------------------------------------------------------------

def test_group_lifecycle(client, seeded):
    gid = client.post("/api/groups", json={"name": "Perimeter"}).json()["id"]
    assert client.post(f"/api/groups/{gid}/sensors/cam.front").status_code == 200
    assert client.post(f"/api/groups/{gid}/sensors/cam.rear").status_code == 200

    groups = client.get("/api/groups").json()
    assert [s["id"] for s in groups[0]["sensors"]] == ["cam.rear", "cam.front"]
    assert client.get("/api/sensors", params={"group_id": gid}).json()["total"] == 2

    client.delete(f"/api/groups/{gid}/sensors/cam.rear")
    assert len(client.get("/api/groups").json()[0]["sensors"]) == 1

    client.patch(f"/api/groups/{gid}", json={"name": "Outside", "collapsed": True})
    g = client.get("/api/groups").json()[0]
    assert g["name"] == "Outside" and g["collapsed"] == 1

    client.delete(f"/api/groups/{gid}")
    assert client.get("/api/groups").json() == []


def test_group_errors_are_400(client, seeded):
    client.post("/api/groups", json={"name": "Dup"})
    assert client.post("/api/groups", json={"name": "Dup"}).status_code == 400
    assert client.post("/api/groups", json={"name": "  "}).status_code == 400
    gid = client.get("/api/groups").json()[0]["id"]
    assert client.post(f"/api/groups/{gid}/sensors/ghost").status_code == 400


def test_deleting_a_group_keeps_its_sensors(client, seeded):
    gid = client.post("/api/groups", json={"name": "Temp"}).json()["id"]
    client.post(f"/api/groups/{gid}/sensors/cam.front")
    client.delete(f"/api/groups/{gid}")
    assert client.get("/api/sensors/cam.front").status_code == 200


# --- alarms ----------------------------------------------------------------

def test_alarm_toggle(client, seeded):
    rule_id = seeded["rule_id"]
    assert client.get("/api/alarms").json()[0]["armed"] == 0
    assert client.post(f"/api/alarms/{rule_id}/armed", json={"armed": True}).json()["armed"] == 1
    assert client.get("/api/alarms", params={"armed": True}).json()[0]["id"] == rule_id
    client.post("/api/alarms/all", json={"armed": False})
    assert client.get("/api/alarms").json()[0]["armed"] == 0


def test_alarm_search(client, seeded):
    assert len(client.get("/api/alarms", params={"q": "motion"}).json()) == 1
    assert client.get("/api/alarms", params={"q": "zzz"}).json() == []


# --- verdicts --------------------------------------------------------------

def test_verdict_is_recorded_and_validated(client, seeded):
    eid = seeded["escalation_id"]
    r = client.post(f"/api/escalations/{eid}/verdict",
                    json={"verdict": "false_positive", "note": "That is the postman"})
    assert r.status_code == 200
    assert client.post(f"/api/escalations/{eid}/verdict",
                       json={"verdict": "nonsense"}).status_code == 400
    detail = client.get(f"/api/escalations/{eid}").json()
    assert detail["verdicts"][0]["note"] == "That is the postman"


def test_escalation_status_transitions(client, seeded):
    eid = seeded["escalation_id"]
    assert client.patch(f"/api/escalations/{eid}",
                        json={"status": "acknowledged"}).json()["status"] == "acknowledged"
    assert client.patch(f"/api/escalations/{eid}",
                        json={"status": "sideways"}).status_code == 400


# --- the click/speak equivalence ------------------------------------------

async def test_every_rest_action_has_a_matching_tool(data_dir):
    """The point of the shared service layer: anything the dashboard can do,
    the assistant can do too."""
    tools = set(register_core_tools(ToolRegistry()).tools)
    for expected in [
        "list_sensors", "get_sensor", "search_events", "list_escalations",
        "get_escalation", "set_status", "record_verdict", "list_alarms",
        "arm_alarm", "disarm_alarm", "set_all_alarms", "list_groups",
        "create_group", "rename_group", "delete_group",
        "add_sensor_to_group", "remove_sensor_from_group",
    ]:
        assert expected in tools, expected


async def test_tool_and_rest_return_the_same_rows(client, seeded):
    from blackice.services import events as svc

    via_rest = client.get("/api/events", params={"q": "person"}).json()
    via_tool = await svc.list_events(q="person")
    assert [r["id"] for r in via_rest["rows"]] == [r["id"] for r in via_tool["rows"]]


# --- single-process serving ------------------------------------------------

def test_media_requires_authentication(data_dir):
    """Captured footage is from inside a home; a bare static mount would hand
    it to anyone on the LAN."""
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    (data_dir / "media").mkdir(parents=True, exist_ok=True)
    (data_dir / "media" / "frame.jpg").write_bytes(b"\xff\xd8\xff")

    with TestClient(app) as anon:
        assert anon.get("/media/frame.jpg").status_code == 401


def test_media_is_served_to_a_signed_in_user(client, data_dir):
    (data_dir / "media").mkdir(parents=True, exist_ok=True)
    (data_dir / "media" / "frame.jpg").write_bytes(b"\xff\xd8\xffbody")

    r = client.get("/media/frame.jpg")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8\xffbody"


async def test_media_rejects_path_traversal(client, data_dir):
    """`..` must not escape the media root into the database or .env.

    Asserted on the handler as well as over HTTP, because httpx normalises `..`
    client-side and an HTTP-only check can pass without the guard existing.
    """
    (data_dir / "secret.txt").write_text("PASSWORD")
    (data_dir / "media").mkdir(parents=True, exist_ok=True)

    from fastapi import HTTPException

    from blackice.api.static import media

    for attempt in ["../secret.txt", "../../etc/passwd", "sub/../../secret.txt"]:
        with pytest.raises(HTTPException) as caught:
            await media(attempt)
        assert caught.value.status_code == 404, attempt

    # Over HTTP the status varies -- httpx normalises `../` away client-side, so
    # the request becomes an unrelated path the SPA answers. What must hold in
    # every case is that the file's contents never come back.
    for attempt in ["../secret.txt", "..%2fsecret.txt", "%2e%2e%2fsecret.txt"]:
        assert b"PASSWORD" not in client.get(f"/media/{attempt}").content, attempt


def test_missing_media_is_404_not_500(client, data_dir):
    assert client.get("/media/nope.jpg").status_code == 404
