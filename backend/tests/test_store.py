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
