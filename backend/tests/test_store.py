"""Чтение файла и события: имитируем то, что жюри делает с файлом на защите."""
from datetime import timedelta

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
    store, _ = make_store([make("K", 0, 4), make("M", 0, 2), make("L", 2, 1), make("R", 7, 2)])
    snap = store.snapshot()
    assert set(snap["incidents"]) == {"K"} and snap["incidents_scope"] == "visible"
    assert snap["outside_active"] == 2 and snap["active_total"] == 3 and snap["total_incidents"] == 4


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
