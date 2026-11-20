"""Мини-ITSM пишет в файл → дашборд видит изменение; общие тревоги и «в работе»."""
import json

import pytest

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


# ---------- общие тревоги ----------
def test_alerts_ack_shared_and_rearm(make_store):
    store, f = make_store([make("K", 0, 4), make("C", 0, 2, COORD)])
    keys = {a["key"]: a for a in store.snapshot()["alerts"]}
    assert set(keys) == {"critical:K", "coordinators:C"}
    assert store.snapshot()["alerts"][0]["group"] == "critical"  # критические первыми
    assert store.ack("critical:K", "Ольга")
    a = next(x for x in store.snapshot()["alerts"] if x["key"] == "critical:K")
    assert a["acked_by"] == "Ольга"
    # понизили и снова подняли до критического — новый эпизод, «Принял» сброшен
    write(f, [make("K", 0, 3), make("C", 0, 2, COORD)])
    store.poll()
    write(f, [make("K", 0, 4), make("C", 0, 2, COORD)])
    store.poll()
    a = next(x for x in store.snapshot()["alerts"] if x["key"] == "critical:K")
    assert a["acked_by"] is None


def test_claim_cleared_when_incident_leaves_groups(make_store):
    store, f = make_store([make("C", 0, 2, COORD)])
    store.claim("C", "Ольга")
    assert store.snapshot()["claims"]["C"]["by"] == "Ольга"
    write(f, [make("C", 0, 2, "КЦ ДОИ VMWare")])  # передали в команду
    store.poll()
    assert "C" not in store.snapshot()["claims"]


def test_teams_list(make_store):
    store, _ = make_store([make("A", team="КЦ ДОИ VMWare"), make("B", team="AutoClosedMi")])
    teams = store.snapshot()["teams"]
    assert "КЦ ДОИ VMWare" in teams and COORD in teams and "AutoClosedMi" not in teams
