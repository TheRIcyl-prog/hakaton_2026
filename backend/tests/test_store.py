"""Чтение файла и события: имитируем то, что жюри делает с файлом на защите."""
from datetime import timedelta

from backend.app.adapter import iso
from backend.app.history import History
from backend.app.itsm import Itsm
from backend.app.store import Store

from .conftest import COORD, NOW, FakeClock, make, write


def types(store):
    return [e["type"] for e in store.events]


def test_initial_load_has_no_events(make_store):
    store, _ = make_store([make("A"), make("B", 0, 4)])
    assert store.file["status"] == "ok" and len(store.incidents) == 2
    assert list(store.events) == []


def test_priority_to_critical_raises_alert(make_store):
    store, f = make_store([make("A", 0, 3)])
    write(f, [make("A", 0, 4)])
    assert store.poll()
    assert "priority_changed" in types(store) and "became_critical" in types(store)
    ev = next(e for e in store.events if e["type"] == "became_critical")
    assert ev["alert"] == "critical" and ev["incident_id"] == "A"
    assert store.incidents["A"]["groups"] == ["critical"]


def test_new_critical_incident(make_store):
    store, f = make_store([make("A")])
    write(f, [make("A"), make("B", 0, 4)])
    store.poll()
    assert types(store) == ["new", "became_critical"]


def test_assigned_to_coordinators_from_research(make_store):
    store, f = make_store([make("A", 20, 2, "КЦ ИТ Мой МТС")])
    write(f, [make("A", 20, 2, COORD)])
    store.poll()
    assert "team_changed" in types(store) and "to_coordinators" in types(store)
    assert store.incidents["A"]["groups"] == ["coordinators", "research"]


def test_critical_resolved_stays_then_leaves(make_store):
    clock = FakeClock()
    store, f = make_store([make("A", 2, 4)], clock)
    write(f, [make("A", 7, 4)])  # без completionDate — берём момент, когда увидели
    store.poll()
    assert {"resolved", "critical_resolved", "status_changed"} <= set(types(store))
    assert store.incidents["A"]["resolved_in"] == ["critical"]
    clock._now = NOW + timedelta(minutes=31)
    assert store.poll()  # файл не менялся, но группа пересчиталась по времени
    assert store.groups["critical"] == []


def test_finish_windows_from_each_transition_survive_restart(make_store, tmp_path):
    clock = FakeClock()
    owner = {"name": "Ольга", "at": iso(NOW)}
    store, path = make_store([make("HIGH", 2, 3), make("MINE", 2, 2, coordinator=owner, isNeedUp=True),
                              make("RESEARCH", 20, 2)], clock)
    history_file = tmp_path / "switches.jsonl"
    store.history = History(history_file)
    itsm = Itsm(path, store.rules, clock.now, store=store)
    for inc_id in ("HIGH", "MINE"):
        itsm.set_state(inc_id, 7, "Ольга")
    clock._now = NOW + timedelta(minutes=29)
    store.poll()
    assert store.groups["high"] == ["HIGH"]
    assert "MINE" in store.snapshot()["incidents"]
    clock._now = NOW + timedelta(minutes=30)
    assert store.poll()
    assert store.groups["high"] == []
    assert "MINE" not in store.snapshot()["incidents"]

    for inc_id in ("HIGH", "MINE", "RESEARCH"):
        itsm.set_state(inc_id, 10, "Ольга")
    closed = Store(store.settings, store.rules, clock, history=History(history_file))
    assert closed.poll()
    assert closed.groups["high"] == ["HIGH"] and closed.groups["research"] == ["RESEARCH"]
    assert closed.snapshot()["incidents"]["HIGH"]["resolved_at"] == iso(NOW)
    assert "MINE" in closed.snapshot()["incidents"]
    assert any(item["id"] == "aftermath:MINE" for item in closed.queue("Ольга"))

    clock._now = NOW + timedelta(minutes=30, seconds=9)
    closed.poll()
    assert closed.groups["high"] == ["HIGH"] and closed.groups["research"] == ["RESEARCH"]
    assert closed.snapshot()["incidents"]["HIGH"]["closed_until"] == (clock.now() + timedelta(seconds=1)).isoformat()
    clock._now = NOW + timedelta(minutes=30, seconds=10)
    assert closed.poll()
    assert not closed.groups["high"] and not closed.groups["research"]
    assert not (closed.snapshot()["incidents"].keys() & {"HIGH", "MINE", "RESEARCH"})
    assert not any(item["id"] == "aftermath:MINE" for item in closed.queue("Ольга"))
    assert closed.incident("MINE")["state_kind"] == "closed"


def test_recently_closed_incidents_stay_searchable_and_survive_restart(make_store, monkeypatch, coordinator_clients):
    from backend.app import main
    from fastapi.testclient import TestClient

    clock = FakeClock()
    day = NOW - timedelta(hours=4)
    records = [
        make("LIVE", 7, 4, completionDate=iso(day)),
        make("IMPORTED", 10, 3, completionDate=iso(NOW - timedelta(hours=1))),
        make("OLD", 10, 3, completionDate=iso(day)),
    ]
    store, path = make_store(records, clock)
    history_path = path.with_name("history.jsonl")
    store.history = History(history_path)
    records[0].update(stateId=10, state="Закрыт", stateEnum="Closed")
    write(path, records)
    assert store.poll()
    clock._now += timedelta(seconds=11)
    assert store.poll() and not store.groups["critical"]

    store = Store(store.settings, store.rules, clock, history=History(history_path))
    assert store.poll()
    assert store.incident("LIVE")["state_kind"] == "closed"
    assert [row["id"] for row in store.recently_closed("incidents")["items"]] == ["LIVE", "IMPORTED"]
    assert store.recently_closed("incidents", limit=1)["next_offset"] == 1
    assert store.recently_closed("incidents", offset=1, limit=1)["previous_offset"] == 0
    monkeypatch.setattr(main, "store", store)
    assert TestClient(main.app).get("/api/recently-closed").status_code == 401
    client = coordinator_clients(main.app, "Ольга")["Ольга"]
    assert client.get("/api/recently-closed").json()["total"] == 2

    records[0].update(stateId=2, state="Выполняется", stateEnum="WorkInProgress")
    write(path, records)
    assert store.poll() and "LIVE" in store.groups["critical"]
    assert store.recently_closed("incidents")["total"] == 1
    clock._now = NOW + timedelta(hours=2, seconds=1)
    assert not store.recently_closed("incidents")["items"]


def test_reopened(make_store):
    store, f = make_store([make("A", 7, 3)])
    write(f, [make("A", 2, 3)])
    store.poll()
    assert "reopened" in types(store)


def test_deadline_and_reassign(make_store):
    store, f = make_store([make("A", 0, 1, COORD)])
    write(f, [make("A", 0, 1, "КЦ ДОИ VMWare", deadline="2026-11-11T00:00:00Z")])
    store.poll()
    t = types(store)
    assert {"deadline_changed", "team_changed", "from_coordinators"} <= set(t)


def test_removed(make_store):
    store, f = make_store([make("A"), make("B")])
    write(f, [make("A")])
    store.poll()
    assert types(store) == ["removed"]


def test_half_written_file_keeps_last_snapshot(make_store):
    store, f = make_store([make("A", 0, 4)])
    good = f.read_text(encoding="utf-8")
    f.write_text(good[: len(good) // 2], encoding="utf-8")
    assert not store.poll()
    assert store.file["status"] == "corrupt" and "A" in store.incidents
    f.write_text("", encoding="utf-8")
    store.poll()
    assert store.file["status"] == "corrupt" and "A" in store.incidents
    write(f, [make("A", 0, 3)])
    store.poll()
    assert store.file["status"] == "ok" and store.incidents["A"]["priority_id"] == 3


def test_missing_file_keeps_last_snapshot(make_store):
    store, f = make_store([make("A")])
    f.unlink()
    store.poll()
    assert store.file["status"] == "missing" and "A" in store.incidents


def test_nonempty_file_without_incident_ids_keeps_last_snapshot(make_store):
    store, f = make_store([make("A", 0, 4)])
    write(f, [{"title": "Запись без номера"}])
    assert not store.poll()
    assert store.file["status"] == "corrupt" and "A" in store.incidents

    write(f, [make("A", 0, 3), {"title": "Запись без номера"}])
    assert store.poll()
    assert store.file["status"] == "ok" and store.file["warnings"]
    assert store.incidents["A"]["priority_id"] == 3

    write(f, [])
    assert store.poll()
    assert store.file["status"] == "ok" and not store.incidents


def test_unchanged_file_no_version_bump(make_store):
    store, _ = make_store([make("A")])
    v = store.version
    store.poll()
    assert store.version == v


def test_snapshot_shape(make_store):
    store, _ = make_store([make("A", 0, 4, COORD)])
    snap = store.snapshot()
    assert [g["key"] for g in snap["groups"]] == ["critical", "coordinators", "high", "research"]
    assert snap["counts"] == {"critical": 1, "coordinators": 1, "high": 0, "research": 0}
    assert snap["incidents"]["A"]["ai"]["route"] is None  # некого предложить — других инцидентов нет


def test_file_replaced_resets_baseline(make_store):
    store, f = make_store([make("A"), make("B"), make("C")])
    write(f, [make("X", 0, 4), make("Y"), make("Z")])
    store.poll()
    assert list(store.events) == []  # подмена файла — новая точка отсчёта, не 6 событий
    assert set(store.incidents) == {"X", "Y", "Z"}
    write(f, [make("X", 0, 4), make("Y", 0, 3), make("Z")])
    store.poll()
    assert "priority_changed" in types(store)


# ---------- лёгкий снимок, поиск и фильтры по всем инцидентам (decisions.md, п. 3, 6) ----------
def test_snapshot_has_only_visible_and_outside_counter(make_store):
    store, _ = make_store([make("K", 0, 4), make("M", 0, 2), make("L", 2, 1), make("R", 7, 2),
                           make("C", 0, 2, team=COORD)])
    snap = store.snapshot()
    assert set(snap["incidents"]) == {"K", "C"} and snap["incidents_scope"] == "visible"
    assert snap["outside_active"] == 2 and snap["active_total"] == 4 and snap["total_incidents"] == 5
    outside = store.search(group="outside")
    assert outside["total"] == snap["outside_active"]
    assert {inc["id"] for inc in outside["items"]} == {"M", "L"}


def test_taken_incident_outside_groups_is_visible(make_store):
    store, _ = make_store([make("M", 0, 2, coordinator={"name": "Ольга", "at": "2026-11-10T11:00:00Z"})])
    snap = store.snapshot()
    assert "M" in snap["incidents"] and snap["claims"]["M"]["by"] == "Ольга"


def test_search_by_text_number_and_filters(make_store):
    store, _ = make_store([
        make("INC1", 0, 2, productName="Зеркальце", header="Не доставляются сообщения"),
        make("INC2", 7, 2, productName="Зеркальце", header="Сообщения с одной галочкой"),
        make("INC3", 2, 3, team="КЦ Ступа", assignedEmployee=None),
        make("INC4", 0, 1, deadline="2026-11-10T11:00:00Z", serviceType="Технологический"),
    ])
    assert store.search("сообщения")["total"] == 2
    assert store.search("сообщения", status="active")["total"] == 1
    assert store.search("inc3")["items"][0]["id"] == "INC3"
    assert [i["id"] for i in store.search(team="КЦ Ступа")["items"]] == ["INC3"]
    assert [i["id"] for i in store.search(unassigned=True)["items"]] == ["INC3"]
    assert [i["id"] for i in store.search(overdue=True)["items"]] == ["INC4"]
    assert [i["id"] for i in store.search(product="Зеркальце", status="done")["items"]] == ["INC2"]
    assert {i["id"] for i in store.search(service_type="internal")["items"]} == {"INC1", "INC2", "INC3", "INC4"}  # в make нет serviceType
    assert store.search(service_type="client")["total"] == 0
    assert [i["id"] for i in store.search(priority="3,4")["items"]] == ["INC3"]
    res = store.search(limit=2)
    assert res["total"] == 4 and len(res["items"]) == 2
    assert res["items"][0]["state_kind"] == "active" and res["items"][0]["priority_id"] == 3  # активные, выше приоритет


def test_search_untaken_and_coordinator(make_store):
    store, _ = make_store([make("K", 0, 4), make("T", 0, 4, coordinator={"name": "Ольга", "at": None}), make("M", 0, 2)])
    assert [i["id"] for i in store.search(untaken=True)["items"]] == ["K"]
    assert [i["id"] for i in store.search(coordinator="ольга")["items"]] == ["T"]


def test_coordinators_ordered_by_waiting_time(make_store):
    store, _ = make_store([
        make("NEW", 0, 4, COORD, lastAssignmentToTeamDateTime="2026-11-10T11:55:00Z"),
        make("OLD", 0, 2, COORD, lastAssignmentToTeamDateTime="2026-11-10T11:10:00Z"),
        make("MID", 2, 3, COORD, lastAssignmentToTeamDateTime="2026-11-10T11:30:00Z"),
    ])
    assert store.groups["coordinators"] == ["OLD", "MID", "NEW"]  # дольше ждёт — выше


def test_research_newest_first(make_store):
    store, _ = make_store([
        make("R1", 20, 2, completionDate="2026-11-10T09:00:00Z"),
        make("R2", 20, 2, completionDate="2026-11-10T11:00:00Z"),
    ])
    assert store.groups["research"] == ["R2", "R1"]


def test_frequent_foreign_edits_are_coalesced(make_store):
    store, f = make_store([make("A", 0, 2)])
    store.settings.coalesce_seconds = 0.5
    store._last_foreign -= 1            # с первой загрузки прошло больше 0,5 с
    write(f, [make("A", 0, 3)])
    assert store.poll()                 # первая правка — сразу
    write(f, [make("A", 0, 4)])
    assert not store.poll()             # вторая через миг — склеиваем
    store._last_foreign -= 1
    assert store.poll() and store.incidents["A"]["priority_id"] == 4


def test_unchanged_records_are_not_re_adapted(make_store):
    store, f = make_store([make("A"), make("B")])
    a_obj = store.incidents["A"]
    write(f, [make("A"), make("B", 0, 3)])
    store.poll()
    assert store.incidents["A"] is a_obj and store.refs.changed == {"B"}


def test_snapshot_bytes_cached_per_version(make_store):
    store, f = make_store([make("A", 0, 4)])
    b1, v1 = store.snapshot_bytes(True)
    b2, v2 = store.snapshot_bytes(True)
    assert b1 is b2 and v1 == v2
    write(f, [make("A", 0, 3)])
    store.poll()
    assert store.snapshot_bytes(True)[1] > v1


def test_ai_dirty_reenriches_without_forecast(make_store, monkeypatch):
    from backend.app import store as store_mod
    store, _ = make_store([make("A", 0, 3)])
    calls = {"forecast": 0, "enrich": 0}
    monkeypatch.setattr(store_mod, "forecast_all", lambda *a, **k: calls.__setitem__("forecast", calls["forecast"] + 1))
    monkeypatch.setattr(store_mod, "enrich", lambda *a, **k: calls.__setitem__("enrich", calls["enrich"] + 1))
    monkeypatch.setattr(store_mod.ai_layer, "consume_dirty", lambda: True)
    v = store.version
    store._last_regroup = None
    assert store.poll() and store.version > v
    assert calls == {"forecast": 0, "enrich": 1}


def test_history_recorded_before_ai_sees_the_edit(make_store, monkeypatch):
    from backend.app import store as store_mod
    store, f = make_store([make("A", 2, 2)])
    seen = []
    monkeypatch.setattr(store_mod, "enrich", lambda incs, now, rules, **k: seen.append(len(store.history.get("A"))))
    write(f, [make("A", 2, 3)])
    store.poll()
    assert seen == [1]  # ИИ на этой же правке уже видит повышение приоритета
