"""Мини-ITSM пишет в файл → дашборд видит изменение; общие тревоги и «в работе»."""
import json

import pytest
from fastapi.testclient import TestClient

from backend.app.itsm import Itsm, ItsmError

from .conftest import COORD, NOW, make, write


@pytest.fixture
def env(make_store, rules):
    def _env(records):
        store, f = make_store(records)
        return store, Itsm(f, rules, lambda: NOW), f

    return _env


def raw(f, inc_id):
    return next(r for r in json.loads(f.read_text(encoding="utf-8")) if r["id"] == inc_id)


def types(store):
    return [e["type"] for e in store.events]


def test_assign_from_coordinators_writes_file_and_events(env):
    store, itsm, f = env([make("A", 2, 2, COORD)])
    itsm.assign("A", "КЦ ДОИ VMWare", "Ольга", comment="по похожим случаям")
    r = raw(f, "A")
    assert r["assignedGroupName"] == "КЦ ДОИ VMWare" and r["stateId"] == 0 and r["state"] == "Назначен"
    assert r["lastAssignmentToTeamDateTime"] == "2026-11-10T12:00:00Z"
    assert r["workLog"][-1]["author"] == "Ольга" and "КЦ ДОИ VMWare" in r["workLog"][-1]["text"]
    store.poll()
    assert {"team_changed", "from_coordinators", "status_changed"} <= set(types(store))
    assert all(e.get("by") == "Ольга" for e in store.events)
    assert store.incidents["A"]["groups"] == []


def test_assign_keeps_research_state(env):
    store, itsm, f = env([make("R", 20, 2, "КЦ ИТ Мой МТС")])
    itsm.assign("R", COORD, "Ольга")
    store.poll()
    assert raw(f, "R")["stateId"] == 20
    assert store.incidents["R"]["groups"] == ["coordinators", "research"]
    assert "to_coordinators" in types(store)


def test_state_transitions(env):
    store, itsm, f = env([make("A", 0, 4)])
    with pytest.raises(ItsmError, match="можно перевести только"):
        itsm.set_state("A", 10, "Ольга")  # из «Назначен» сразу в «Закрыт» нельзя
    itsm.set_state("A", 7, "Ольга", reason="Постоянное решение")
    r = raw(f, "A")
    assert r["stateId"] == 7 and r["completionDate"] == "2026-11-10T12:00:00Z"
    store.poll()
    assert "critical_resolved" in types(store)
    assert store.incidents["A"]["resolved_in"] == ["critical"]
    itsm.set_state("A", 2, "Ольга")  # вернули в работу
    assert raw(f, "A")["completionDate"] is None
    store.poll()
    assert "reopened" in types(store)


def test_closed_is_final(env):
    _, itsm, _ = env([make("A", 10, 2)])
    with pytest.raises(ItsmError, match="закрыт"):
        itsm.assign("A", "КЦ ИТ Мой МТС", "Ольга")
    with pytest.raises(ItsmError):
        itsm.set_state("A", 2, "Ольга")


def test_status_api_updates_exported_enum_and_checks_drawer_hash(make_store, monkeypatch):
    from backend.app import main

    record = make("A", 0, 3)
    del record["stateId"]
    store, path = make_store([record])
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "itsm", Itsm(path, store.rules, store.clock.now, store=store))
    client = TestClient(main.app)
    url = "/api/itsm/A/state"
    original = client.get("/api/incidents/A").json()
    assert client.get("/api/meta").json()["transitions"]["0"] == [2, 7]

    body = {"by": "Ольга", "state_id": 2, "expected_hash": original["hash"]}
    running = client.post(url, json=body)
    assert running.status_code == 200 and running.json()["state_id"] == 2
    body.update(state_id=7, reason="Постоянное решение", comment="Подтверждено исполнителем")
    assert client.post(url, json=body).status_code == 409
    body["expected_hash"] = running.json()["hash"]
    resolved = client.post(url, json=body)
    assert resolved.status_code == 200 and resolved.json()["state_id"] == 7
    assert raw(path, "A")["stateReason"] == body["reason"]
    assert body["comment"] in raw(path, "A")["workLog"][-1]["text"]
    closed = client.post(url, json={**body, "state_id": 10, "expected_hash": resolved.json()["hash"]})
    assert closed.status_code == 200 and closed.json()["state_id"] == 10


def test_priority_with_sla_recalc(env):
    store, itsm, f = env([make("A", 2, 3, submitDate="2026-11-10T11:00:00Z")])
    itsm.set_priority("A", 4, "Ольга", recalc_deadline=True)
    r = raw(f, "A")
    assert r["priorityId"] == 4 and r["priority"] == "Наивысший" and r["deadline"] == "2026-11-10T13:00:00Z"
    store.poll()
    assert "became_critical" in types(store)


def test_comment_and_escalate_events(env):
    store, itsm, _ = env([make("A", 2, 3)])
    itsm.comment("A", "команда подтвердила приём", "Ольга")
    itsm.escalate("A", "Сергей")
    store.poll()
    assert types(store) == ["comment", "escalated"]
    assert store.events[-1]["by"] == "Сергей"
    assert len(store.incidents["A"]["work_log"]) == 2


def test_requires_name_and_valid_file(env):
    store, itsm, f = env([make("A")])
    with pytest.raises(ItsmError, match="имя"):
        itsm.comment("A", "x", " ")
    f.write_text('[{"id": "A", "битый', encoding="utf-8")
    with pytest.raises(ItsmError) as e:
        itsm.comment("A", "x", "Ольга")
    assert e.value.status == 409
    assert f.read_text(encoding="utf-8").startswith('[{"id": "A", "битый')  # файл жюри не тронули


def test_unknown_fields_preserved(env):
    _, itsm, f = env([make("A", customField={"x": 1})])
    itsm.comment("A", "проверка", "Ольга")
    assert raw(f, "A")["customField"] == {"x": 1}


def test_import_validates_and_backs_up(env, tmp_path):
    store, itsm, f = env([make("A")])
    with pytest.raises(ItsmError):
        itsm.import_file("не json", tmp_path / "b")
    n = itsm.import_file(json.dumps([make("X"), make("Y")]), tmp_path / "b")
    assert n == 2 and len(list((tmp_path / "b").iterdir())) == 1
    store.poll()
    assert set(store.incidents) == {"X", "Y"}


# ---------- «Взять в работу» в файле, тревоги, конфликты (decisions.md, п. 4, 7) ----------
@pytest.fixture
def linked(make_store, rules, tmp_path):
    """Store + Itsm, связанные как в main.py: запись применяется в памяти без перечитывания."""
    def _linked(records):
        store, f = make_store(records)
        return store, Itsm(f, rules, lambda: NOW, store=store, backup_dir=tmp_path / "backups", backups_keep=20), f

    return _linked


def alert(store, key):
    return next(a for a in store.snapshot()["alerts"] if a["key"] == key)


def test_take_writes_file_and_clears_alert_for_everyone(linked):
    store, itsm, f = linked([make("K", 0, 4), make("C", 0, 2, COORD)])
    assert {a["key"] for a in store.snapshot()["alerts"]} == {"critical:K", "coordinators:C"}
    assert store.snapshot()["alerts"][0]["group"] == "critical"  # критические первыми
    v = store.version
    itsm.take("K", "Ольга")
    assert raw(f, "K")["coordinator"] == {"name": "Ольга", "at": "2026-11-10T12:00:00Z"}
    assert raw(f, "K")["workLog"][-1]["action"] == "take"
    # применено в памяти сразу, без опроса файла
    assert store.version > v and store.incidents["K"]["coordinator"]["name"] == "Ольга"
    a = alert(store, "critical:K")
    assert a["taken_by"] == "Ольга" and a["acked_by"] == "Ольга"  # acked_by — для старого фронта
    assert store.snapshot()["claims"]["K"]["by"] == "Ольга"
    assert "taken" in types(store)
    assert not store.poll()  # свой файл повторно не разбираем


def test_take_conflicts_and_release_rearms_alert(linked):
    store, itsm, f = linked([make("K", 0, 4)])
    itsm.take("K", "Ольга")
    with pytest.raises(ItsmError, match="уже в работе у Ольга") as e:
        itsm.take("K", "Сергей")
    assert e.value.status == 409
    itsm.take("K", "Сергей", expected_hash=e.value.detail["hash"], force=True)
    assert raw(f, "K")["coordinator"]["name"] == "Сергей"
    itsm.release("K", "Сергей")
    assert "coordinator" not in raw(f, "K")
    assert alert(store, "critical:K")["taken_by"] is None  # отпустили, а он всё ещё критический — тревога снова
    assert "released" in types(store)


def test_alert_new_episode_after_leaving_group(make_store):
    store, f = make_store([make("K", 0, 4)])
    since = alert(store, "critical:K")["since"]
    write(f, [make("K", 0, 3)])
    store.poll()
    assert not store.snapshot()["alerts"]
    write(f, [make("K", 0, 4)])
    store.poll()
    assert alert(store, "critical:K")["taken_by"] is None and alert(store, "critical:K")["since"] >= since


def test_assign_clears_coordinator(linked):
    store, itsm, f = linked([make("C", 0, 2, COORD)])
    itsm.take("C", "Ольга")
    itsm.assign("C", "КЦ ДОИ VMWare", "Ольга")
    r = raw(f, "C")
    assert "coordinator" not in r and "снят с координатора Ольга" in r["workLog"][-1]["text"]
    assert "C" not in store.snapshot()["claims"]


def test_conflict_when_incident_changed_meanwhile(linked):
    store, itsm, f = linked([make("A", 2, 3)])
    seen = store.incidents["A"]["hash"]               # координатор открыл панель
    write(f, [make("A", 2, 4, deadline="2026-11-10T13:00:00Z")])  # а жюри поменяло файл
    store.poll()
    before = f.read_text(encoding="utf-8")
    with pytest.raises(ItsmError) as e:
        itsm.set_priority("A", 2, "Ольга", expected_hash=seen)
    assert e.value.status == 409 and e.value.detail["conflict"]
    fields = {c["field"] for c in e.value.detail["changes"]}
    assert {"priority", "deadline"} <= fields
    assert f.read_text(encoding="utf-8") == before   # ничего не перезаписали
    itsm.set_priority("A", 2, "Ольга", expected_hash=store.incidents["A"]["hash"])  # с актуальной версией — можно
    assert raw(f, "A")["priorityId"] == 2


def test_backup_before_each_write_keeps_20(linked, tmp_path):
    store, itsm, f = linked([make("A", 2, 3)])
    for i in range(23):
        itsm.comment("A", f"комментарий {i}", "Ольга")
    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 20
    assert len(store.incidents["A"]["work_log"]) == 23 or len(store.incidents["A"]["work_log"]) == 30


def test_history_records_changes_with_author(linked):
    store, itsm, f = linked([make("A", 2, 2)])
    itsm.set_priority("A", 3, "Ольга")
    write(f, [{**raw(f, "A"), "priorityId": 4, "priority": "Наивысший"}])  # жюри подняло ещё раз
    store.poll()
    h = store.history.get("A")
    assert [(c["kind"], c["before"], c["after"], c["by"]) for c in h] == [
        ("priority", "Средний", "Высокий", "Ольга"), ("priority", "Высокий", "Наивысший", None)]
    assert all(c["up"] for c in h)
    assert store.incident("A")["history"] == h


def test_history_seeded_from_work_log(make_store):
    log = [{"at": "2026-11-10T11:00:00Z", "author": "Жюри", "action": "priority", "text": "x",
            "kind": "priority", "before": "Низкий", "after": "Средний", "before_id": 1, "after_id": 2, "up": True}]
    store, _ = make_store([make("A", 2, 2, workLog=log)])
    h = store.history.get("A")
    assert len(h) == 1 and h[0]["kind"] == "priority" and h[0]["by"] == "Жюри" and h[0]["up"]


def test_teams_list(make_store):
    store, _ = make_store([make("A", team="КЦ ДОИ VMWare"), make("B", team="AutoClosedMi")])
    teams = store.snapshot()["teams"]
    assert "КЦ ДОИ VMWare" in teams and COORD in teams and "AutoClosedMi" not in teams


def test_confirm_duplicates_one_write(linked, tmp_path):
    store, itsm, f = linked([make("M", 0, 2), make("D1", 0, 2), make("D2", 2, 2), make("X", 0, 2)])
    itsm.confirm_duplicates("M", ["M", "D1", "D2"], "Ольга")
    assert raw(f, "M")["isMaster"] is True and raw(f, "M")["isDouble"] is False
    assert raw(f, "D1")["isDouble"] is True and raw(f, "D2")["isMaster"] is False
    assert "D1" in raw(f, "M")["workLog"][-1]["text"] and raw(f, "D1")["workLog"][-1]["action"] == "duplicates"
    assert "isDouble" not in raw(f, "X")
    assert store.incidents["D2"]["is_double"] and store.incidents["M"]["is_master"]  # применено в памяти
    assert len(list((tmp_path / "backups").iterdir())) == 1                          # одна запись — одна копия
    with pytest.raises(ItsmError):
        itsm.confirm_duplicates("M", ["NOPE"], "Ольга")
