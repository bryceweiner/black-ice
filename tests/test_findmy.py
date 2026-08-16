"""The Find My plugin: parsing Apple's cache, measuring distance, and noticing
when someone's state changed.

The cache format is undocumented and TCC-protected, so these tests build the
files themselves. That makes them a statement about what shapes we tolerate --
the point of the fixtures below is that they disagree with each other on key
spelling and battery format, exactly as the real files do.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from blackice_findmy import (
    DEVICES_SENSOR_ID,
    ITEMS_SENSOR_ID,
    PEOPLE_SENSOR_ID,
    FindMyPlugin,
    cache,
    geo,
)
from blackice_findmy.position import Locator, this_mac

from blackice import db
from blackice.llm.tools import ToolRegistry, project_plugin_tools
from blackice.plugins.registry import Registry
from blackice.services import events

# Bryce's house, and a few places measured from it.
HOME = (37.33182, -122.03118)
NEARBY = (37.33200, -122.03150)      # ~40m: inside the geofence
ACROSS_TOWN = (37.40000, -122.10000) # ~10km: away
FAR = (37.80000, -122.40000)         # ~60km: away, and far from everyone


def ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def now_ms() -> int:
    return ms(datetime.now(UTC))


def write_cache(directory, *, friends=None, devices=None, items=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / cache.FRIENDS_FILE).write_text(json.dumps(friends if friends is not None else []))
    (directory / cache.DEVICES_FILE).write_text(json.dumps(devices if devices is not None else []))
    (directory / cache.ITEMS_FILE).write_text(json.dumps(items if items is not None else []))


def friend(name, coords, *, when=None, handles=("+1-555-123-4567",)):
    # Friends.data nests its people and stamps in milliseconds.
    return {
        "id": f"~{name.lower().replace(' ', '')}",
        "name": name,
        "invitationAcceptedHandles": list(handles),
        "location": {
            "latitude": coords[0],
            "longitude": coords[1],
            "timeStamp": when if when is not None else now_ms(),
            "horizontalAccuracy": 12.0,
        },
        "address": {"label": "Home", "locality": "Cupertino"},
    }


def device(name, coords, *, battery=0.85, status="Normal", this_device=False, when=None):
    return {
        "id": f"dev-{name.lower().replace(' ', '-')}",
        "name": name,
        "batteryLevel": battery,
        "batteryStatus": status,
        "isThisDevice": this_device,
        "location": {
            "latitude": coords[0],
            "longitude": coords[1],
            "timeStamp": when if when is not None else now_ms(),
            "horizontalAccuracy": 30.0,
        },
    }


def item(name, coords, *, battery_status=0, when=None):
    # Items.data spells things differently: an enum battery, and `timestamp`.
    return {
        "identifier": f"item-{name.lower()}",
        "name": name,
        "batteryStatus": battery_status,
        "location": {
            "latitude": coords[0],
            "longitude": coords[1],
            "timestamp": when if when is not None else now_ms(),
            "horizontalAccuracy": 15.0,
        },
        "address": {"formattedAddressLines": ["1 Infinite Loop", "Cupertino"]},
    }


@pytest.fixture
def findmy_env(tmp_path, monkeypatch):
    """A cache directory, a home, and an alias for the person we ask about."""
    directory = tmp_path / "fmipcore"
    monkeypatch.setenv(cache.CACHE_DIR_ENV, str(directory))
    monkeypatch.setenv(geo.HOME_LAT_ENV, str(HOME[0]))
    monkeypatch.setenv(geo.HOME_LON_ENV, str(HOME[1]))
    monkeypatch.setenv(geo.HOME_RADIUS_ENV, "150")
    monkeypatch.setenv(geo.ALIASES_ENV, "wife=Jane Doe,keys=Keys")
    # CoreLocation is never consulted in tests; the fallback path is the one
    # that has to work on any machine running the suite.
    monkeypatch.setattr(
        "blackice_findmy.position._corelocation_blocking", lambda timeout: None
    )
    write_cache(
        directory,
        friends=[friend("Jane Doe", ACROSS_TOWN)],
        devices=[device("Bryce's MacBook Pro", HOME, this_device=True)],
        items=[item("Keys", HOME)],
    )
    return directory


@pytest.fixture
async def reg(data_dir, findmy_env, monkeypatch):
    # Tests rewrite the cache and then call poll() by hand, so the background
    # loop is stubbed out: left running it polls the old fixture cache first
    # and every transition test races it. `test_the_loop_polls_on_its_own`
    # covers the loop itself.
    async def no_loop(self):
        return

    monkeypatch.setattr(FindMyPlugin, "_loop", no_loop)
    r = Registry()
    await r.start_plugin(FindMyPlugin, events.record)
    yield r
    await r.stop_all()


def plugin_of(reg):
    return reg.supervisors["findmy"].plugin


async def emitted(kind=None):
    sql = "SELECT * FROM events"
    if kind:
        sql += f" WHERE kind = '{kind}'"
    return await db.fetchall(sql + " ORDER BY id")


# --- parsing ---------------------------------------------------------------

def test_the_three_files_normalise_into_one_shape(findmy_env):
    subjects = {s.name: s for s in cache.read_all(findmy_env)}

    assert set(subjects) == {"Jane Doe", "Bryce's MacBook Pro", "Keys"}
    assert subjects["Jane Doe"].kind == "person"
    assert subjects["Keys"].kind == "item"
    assert subjects["Jane Doe"].lat == pytest.approx(ACROSS_TOWN[0])
    # Two spellings of the timestamp key, both read.
    assert subjects["Keys"].fix_ts is not None
    assert subjects["Jane Doe"].fix_ts is not None


def test_place_comes_from_whichever_address_shape_is_present(findmy_env):
    subjects = {s.name: s for s in cache.read_all(findmy_env)}

    assert subjects["Jane Doe"].place == "Home"           # a label
    assert "Infinite Loop" in subjects["Keys"].place          # formatted lines


def test_alternate_key_spellings_are_tolerated():
    blob = [{
        "id": "x",
        "deviceDisplayName": "Watch",
        # No nested location: coordinates inline, seconds not milliseconds.
        "lat": HOME[0],
        "lng": HOME[1],
        "timestamp": int(datetime.now(UTC).timestamp()),
    }]
    (subject,) = cache.parse(blob, "device")

    assert subject.name == "Watch"
    assert subject.lat == pytest.approx(HOME[0])
    assert subject.fix_ts is not None


def test_friends_file_may_wrap_its_people_in_an_object():
    blob = {"following": [friend("Jane Doe", HOME)]}
    (subject,) = cache.parse(blob, "person")
    assert subject.name == "Jane Doe"


def test_a_record_without_a_location_is_a_subject_not_a_crash():
    (subject,) = cache.parse([{"id": "a", "name": "Nomad"}], "person")

    assert subject.located is False
    assert subject.age_seconds() is None


def test_battery_is_read_from_both_formats():
    (dev,) = cache.parse([device("Phone", HOME, battery=0.10, status="Low")], "device")
    (tag,) = cache.parse([item("Bag", HOME, battery_status=3)], "item")
    (ok,) = cache.parse([device("Pad", HOME, battery=0.90, status="Normal")], "device")

    assert dev.battery == pytest.approx(0.10) and dev.battery_low
    assert tag.battery_low            # enum 3 == critical
    assert not ok.battery_low


def test_a_missing_file_is_not_fatal(tmp_path):
    directory = tmp_path / "partial"
    directory.mkdir()
    (directory / cache.DEVICES_FILE).write_text(json.dumps([device("Mac", HOME)]))

    assert [s.name for s in cache.read_all(directory)] == ["Mac"]


def test_an_unreadable_cache_says_why(tmp_path):
    with pytest.raises(cache.CacheUnavailable) as caught:
        cache.read_all(tmp_path / "nope")
    assert caught.value.reason == "no_cache"


def test_a_half_written_file_is_reported_as_transient(tmp_path):
    directory = tmp_path / "mid-write"
    directory.mkdir()
    (directory / cache.DEVICES_FILE).write_text('[{"id": "a", "na')

    with pytest.raises(cache.CacheUnavailable) as caught:
        cache.read_all(directory)
    assert caught.value.reason == "corrupt"


# --- geometry and naming ---------------------------------------------------

def test_distance_is_measured_on_the_sphere():
    assert geo.distance_m(*HOME, *NEARBY) < 50
    assert 9_000 < geo.distance_m(*HOME, *ACROSS_TOWN) < 12_000


def test_an_alias_resolves_to_the_person(findmy_env):
    subjects = cache.read_all(findmy_env)
    assert geo.resolve("wife", subjects).name == "Jane Doe"
    assert geo.resolve("Jane", subjects).name == "Jane Doe"


def test_an_alias_may_point_at_a_phone_number(findmy_env, monkeypatch):
    """Find My gets friends' names from Contacts, so often there is no name."""
    monkeypatch.setenv(geo.ALIASES_ENV, "wife=+15551234567")
    nameless = friend("", ACROSS_TOWN, handles=("+1 (555) 123-4567",))
    nameless.pop("name")
    subjects = cache.parse([nameless], "person")

    assert geo.resolve("wife", subjects) is not None


def test_an_ambiguous_name_resolves_to_nothing_rather_than_the_wrong_person():
    subjects = cache.parse(
        [friend("Jane Doe", HOME), friend("Jane Smith", FAR)], "person"
    )
    assert geo.resolve("Jane", subjects) is None


def test_distance_is_spoken_the_way_a_person_would_say_it():
    assert geo.spoken_distance(40) == "right here"
    assert geo.spoken_distance(450) == "450 metres away"
    assert geo.spoken_distance(9_400) == "9.4 km away"


# --- lifecycle -------------------------------------------------------------

async def test_discovery_finds_installed_plugin(data_dir):
    assert "findmy" in [c.name for c in Registry().discover()]


async def test_start_projects_all_three_sensors_and_their_rules(reg, data_dir):
    ids = [r["id"] for r in await db.fetchall("SELECT id FROM sensors ORDER BY id")]
    assert {PEOPLE_SENSOR_ID, DEVICES_SENSOR_ID, ITEMS_SENSOR_ID} <= set(ids)

    rules = {
        r["key"]: r for r in await db.fetchall(
            "SELECT r.key, s.armed FROM alarm_rules r"
            " JOIN alarm_state s ON s.rule_id = r.id WHERE r.plugin = 'findmy'"
        )
    }
    assert {"home_arrival", "location_stale", "battery_low", "item_separated"} <= set(rules)
    # Arrivals fire several times a day, so that one starts disarmed.
    assert not rules["home_arrival"]["armed"]
    assert rules["location_stale"]["armed"]
    assert rules["item_separated"]["armed"]


async def test_a_protected_cache_makes_the_plugin_unhealthy_with_a_reason(
    data_dir, monkeypatch, tmp_path
):
    """No Full Disk Access is the owner's to fix, and must be visible."""
    monkeypatch.setenv(cache.CACHE_DIR_ENV, str(tmp_path / "absent"))
    r = Registry()
    await r.start_plugin(FindMyPlugin, events.record)
    try:
        health = r.supervisors["findmy"].health()
        assert health["state"] == "unhealthy"
        assert "Find My" in health["last_error"] or "does not exist" in health["last_error"]
    finally:
        await r.stop_all()


async def test_stop_is_idempotent(reg):
    plugin = plugin_of(reg)
    await plugin.stop()
    await plugin.stop()


async def test_the_loop_polls_on_its_own(data_dir, findmy_env, monkeypatch):
    """The one test that runs the real background loop the fixture stubs out."""
    monkeypatch.setenv("FINDMY_POLL_SECONDS", "0.05")
    r = Registry()
    await r.start_plugin(FindMyPlugin, events.record)
    try:
        plugin = r.supervisors["findmy"].plugin
        for _ in range(50):
            if plugin.readings:
                break
            await asyncio.sleep(0.02)
        assert [x.name for x in plugin.readings if x.subject.kind == "person"] == [
            "Jane Doe"
        ]
        assert plugin.last_ok is not None
    finally:
        await r.stop_all()
    assert plugin.task is None


# --- tools -----------------------------------------------------------------

async def test_where_is_answers_with_place_and_distance(reg):
    result = await reg.command("findmy", "where_is", subject="wife")

    assert result["name"] == "Jane Doe"
    assert result["at_home"] is False
    assert 9_000 < result["distance_home_m"] < 12_000
    # Measured from this Mac, which the cache places at home.
    assert 9_000 < result["distance_me_m"] < 12_000
    assert "Jane Doe" in result["spoken"]
    assert "km away" in result["spoken"]


async def test_where_is_says_someone_is_home_rather_than_quoting_metres(
    reg, findmy_env
):
    write_cache(
        findmy_env,
        friends=[friend("Jane Doe", NEARBY)],
        devices=[device("Bryce's MacBook Pro", HOME, this_device=True)],
    )
    await plugin_of(reg).poll()

    result = await reg.command("findmy", "where_is", subject="wife")
    assert result["at_home"] is True
    assert result["spoken"].startswith("Jane Doe is home")


async def test_where_is_locates_an_item_too(reg):
    result = await reg.command("findmy", "where_is", subject="keys")
    assert result["kind"] == "item"
    assert result["at_home"] is True


async def test_a_stale_position_is_flagged_not_presented_as_current(reg, findmy_env):
    old = ms(datetime.now(UTC) - timedelta(hours=6))
    write_cache(findmy_env, friends=[friend("Jane Doe", ACROSS_TOWN, when=old)])
    await plugin_of(reg).poll()

    result = await reg.command("findmy", "where_is", subject="wife")

    assert result["stale"] is True
    assert "hours ago" in result["reported"]
    assert "out of date" in result["spoken"]


async def test_who_is_home_splits_the_household(reg, findmy_env):
    write_cache(
        findmy_env,
        friends=[friend("Jane Doe", NEARBY), friend("Sam Weiner", ACROSS_TOWN)],
    )
    await plugin_of(reg).poll()

    result = await reg.command("findmy", "who_is_home")

    assert result["home"] == ["Jane Doe"]
    assert result["away"] == ["Sam Weiner"]
    assert "Jane Doe is home" in result["spoken"]


async def test_list_subjects_can_be_filtered(reg):
    everything = await reg.command("findmy", "list_subjects")
    people = await reg.command("findmy", "list_subjects", kind="person")

    assert everything["count"] == 3
    assert people["count"] == 1
    assert people["aliases"]["wife"] == "Jane Doe"


async def test_an_unknown_name_offers_the_real_ones_and_stays_healthy(reg):
    result = await reg.command("findmy", "where_is", subject="the dog")

    assert "no single match" in result["error"]
    assert "Jane Doe" in result["known"]
    assert reg.supervisors["findmy"].health()["state"] == "healthy"


async def test_a_bad_kind_is_an_error_not_a_failure(reg):
    result = await reg.command("findmy", "list_subjects", kind="spaceship")

    assert "unknown kind" in result["error"]
    assert reg.supervisors["findmy"].health()["state"] == "healthy"


async def test_missing_home_coordinates_are_explained_not_guessed(reg, monkeypatch):
    monkeypatch.delenv(geo.HOME_LAT_ENV)
    monkeypatch.delenv(geo.HOME_LON_ENV)

    result = await reg.command("findmy", "who_is_home")

    assert "no home coordinate" in result["error"]
    assert "HOME_LAT" in result["spoken"]
    assert reg.supervisors["findmy"].health()["state"] == "healthy"


async def test_a_cache_that_vanishes_mid_session_advises_rather_than_raises(
    reg, findmy_env, monkeypatch
):
    plugin = plugin_of(reg)
    await plugin.poll()
    plugin.readings = []
    monkeypatch.setenv(cache.CACHE_DIR_ENV, str(findmy_env.parent / "gone"))

    result = await reg.command("findmy", "where_is", subject="wife")

    assert "error" in result
    assert reg.supervisors["findmy"].health()["state"] == "healthy"


# --- events ----------------------------------------------------------------

async def test_first_sight_announces_nothing(reg):
    await plugin_of(reg).poll()
    assert await emitted() == []


async def test_arrival_and_departure_are_emitted_on_the_crossing(reg, findmy_env):
    plugin = plugin_of(reg)
    await plugin.poll()  # Jane is away; state recorded

    write_cache(findmy_env, friends=[friend("Jane Doe", NEARBY)])
    await plugin.poll()

    arrivals = await emitted("arrival")
    assert len(arrivals) == 1
    assert arrivals[0]["sensor_id"] == PEOPLE_SENSOR_ID
    # The name came from Apple, so it is sensor input, not our own words.
    assert "Jane Doe" not in arrivals[0]["summary"]
    assert "Jane Doe" in arrivals[0]["sensor_text"]

    write_cache(findmy_env, friends=[friend("Jane Doe", ACROSS_TOWN)])
    await plugin.poll()
    assert len(await emitted("departure")) == 1


async def test_staying_put_emits_nothing_further(reg, findmy_env):
    plugin = plugin_of(reg)
    await plugin.poll()
    write_cache(findmy_env, friends=[friend("Jane Doe", NEARBY)])
    await plugin.poll()
    await plugin.poll()
    await plugin.poll()

    assert len(await emitted("arrival")) == 1


async def test_a_low_battery_is_reported_once(reg, findmy_env):
    plugin = plugin_of(reg)
    write_cache(findmy_env, devices=[device("Jane's iPhone", ACROSS_TOWN, battery=0.80)])
    await plugin.poll()

    write_cache(findmy_env, devices=[device("Jane's iPhone", ACROSS_TOWN, battery=0.05,
                                            status="Low")])
    await plugin.poll()
    await plugin.poll()

    low = await emitted("battery_low")
    assert len(low) == 1
    assert low[0]["sensor_id"] == DEVICES_SENSOR_ID


async def test_going_quiet_is_reported_separately_from_being_home(reg, findmy_env):
    """"She is home" and "we stopped hearing from her" must never look alike."""
    plugin = plugin_of(reg)
    await plugin.poll()

    old = ms(datetime.now(UTC) - timedelta(hours=4))
    write_cache(findmy_env, friends=[friend("Jane Doe", ACROSS_TOWN, when=old)])
    await plugin.poll()

    stale = await emitted("location_stale")
    assert len(stale) == 1
    assert db.loads(stale[0]["payload"])["age_seconds"] > 3600


async def test_an_item_left_behind_is_the_one_that_is_actionable(reg, findmy_env):
    plugin = plugin_of(reg)
    await plugin.poll()

    # Everyone drove off; the keys stayed on the hook.
    write_cache(
        findmy_env,
        friends=[friend("Jane Doe", FAR)],
        devices=[device("Bryce's MacBook Pro", FAR, this_device=True)],
        items=[item("Keys", HOME)],
    )
    await plugin.poll()

    separated = await emitted("item_separated")
    assert len(separated) == 1
    assert separated[0]["sensor_id"] == ITEMS_SENSOR_ID
    assert separated[0]["severity"] == 2  # medium


async def test_history_is_recorded_for_located_subjects_only(reg, findmy_env):
    plugin = plugin_of(reg)
    nameless = friend("Ghost", HOME)
    nameless.pop("location")
    write_cache(findmy_env, friends=[friend("Jane Doe", ACROSS_TOWN), nameless])
    await plugin.poll()

    rows = await plugin.ctx.db.execute_fetchall("SELECT name FROM positions")
    assert [r["name"] for r in rows] == ["Jane Doe"]


# --- position --------------------------------------------------------------

def test_this_mac_is_found_by_apple_s_own_flag(findmy_env):
    subjects = cache.read_all(findmy_env)
    assert this_mac(subjects).name == "Bryce's MacBook Pro"


def test_this_mac_can_be_named_outright(monkeypatch):
    monkeypatch.setenv("FINDMY_THIS_DEVICE", "Studio")
    subjects = cache.parse([device("Studio", HOME), device("Air", FAR)], "device")
    assert this_mac(subjects).name == "Studio"


async def test_position_falls_back_to_findmy_when_corelocation_is_silent(findmy_env):
    """The common case on this machine: a bare interpreter is never granted
    Location Services, so the fallback is the path that actually runs."""
    fix = await Locator().fix(cache.read_all(findmy_env))

    assert fix.source == "findmy"
    assert fix.lat == pytest.approx(HOME[0])


async def test_a_stale_fix_beats_refusing_to_answer(findmy_env):
    locator = Locator()
    await locator.fix(cache.read_all(findmy_env))

    fix = await locator.fix([])  # nothing locatable this time
    assert fix.source == "cached"
    assert fix.lat == pytest.approx(HOME[0])


# --- dashboard and the model ----------------------------------------------

async def test_every_widget_data_source_returns_data(reg, findmy_env):
    await plugin_of(reg).poll()

    for sensor_id in (PEOPLE_SENSOR_ID, DEVICES_SENSOR_ID, ITEMS_SENSOR_ID):
        for widget in reg.descriptor_for(sensor_id).widgets:
            assert widget.data_source, f"{sensor_id} has a widget with no source"
            assert await reg.query("findmy", widget.data_source) is not None

    people_map = await reg.query("findmy", "people_map")
    labels = [p["label"] for p in people_map["points"]]
    assert "Jane Doe" in labels and "Home" in labels
    # The old single-point shape still works for renderers expecting it.
    assert people_map["lat"] is not None

    assert (await reg.query("findmy", "home_away"))["Jane Doe"] == "away"
    assert (await reg.query("findmy", "people_table"))[0]["Name"] == "Jane Doe"
    assert (await reg.query("findmy", "cache_state"))["state"] == "healthy"


async def test_the_distance_chart_returns_newest_first(reg):
    plugin = plugin_of(reg)
    await plugin.poll()
    await plugin.poll()

    rows = await reg.query("findmy", "distance_history")

    assert rows and set(rows[0]) == {"bucket", "km"}
    assert rows[0]["km"] == pytest.approx(10.0, abs=2.0)


async def test_every_tool_reaches_the_llm(reg):
    tools = ToolRegistry()
    project_plugin_tools(reg, tools)

    assert {"findmy.where_is", "findmy.who_is_home", "findmy.list_subjects"} <= set(tools.tools)
    assert (await tools.dispatch("findmy.where_is", {"subject": "wife"}))["name"] == "Jane Doe"
    assert (await tools.dispatch("findmy.list_subjects", {}))["count"] == 3


def test_the_tool_description_tells_the_model_the_aliases(findmy_env):
    people = FindMyPlugin().describe()[0]
    where_is = next(t for t in people.tools if t.name == "where_is")

    assert "wife" in where_is.parameters["properties"]["subject"]["description"]
