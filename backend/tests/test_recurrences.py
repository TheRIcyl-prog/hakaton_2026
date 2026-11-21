from datetime import timedelta

from backend.app.adapter import iso
from scripts import make_demo

from .conftest import COORD, NOW, make, write


def previous(inc_id, completed, *, team="КЦ Ремонт", state_id=7, reason="Отрегулирован", **fields):
    return make(inc_id, state_id, team=team, submitDate=iso(completed - timedelta(hours=1)),
                completionDate=iso(completed), stateReason=reason, **fields)


def test_recurrence_window_counts_all_cases_and_recommends_latest_eligible_team(make_store):
    current = make("CURRENT", team=COORD, submitDate=iso(NOW))
    just_before = NOW - timedelta(microseconds=1)
    week_before = NOW - timedelta(days=7)
    history = [
        previous("BOUNDARY", week_before, team="КЦ Дежурная", reason="Постоянное решение"),
        previous("ROUTE", NOW - timedelta(days=6)),
        previous("SHOW-5", NOW - timedelta(days=3), team=None),
        previous("SHOW-4", NOW - timedelta(days=1), team=COORD),
        previous("SHOW-3", NOW - timedelta(hours=8), team=COORD.lower(), state_id=20),
        previous("SHOW-2", NOW - timedelta(hours=2), team=None, state_id=10),
        previous("SHOW-1", just_before, team="autoclosedmi", solution="Восстановили вручную"),
    ]
    ignored = [
        previous("TOO-OLD", week_before - timedelta(microseconds=1)),
        previous("SAME-TIME", NOW),
        previous("AFTER", NOW + timedelta(microseconds=1)),
        previous("ACTIVE", just_before, state_id=2),
        previous("OTHER-SERVICE", just_before, service="Другой сервис"),
        previous("OTHER-SCENARIO", just_before, scenarioName="Другой сценарий"),
        previous("NO-SCENARIO", just_before, scenarioName=None),
        make("MISSING-DATE", 7),
        make("BAD-DATE", 10, completionDate="ошибка"),
    ]
    store, _ = make_store([current, *history, *ignored])
    recurrence = store.snapshot()["incidents"]["CURRENT"]["recurrence"]
    assert recurrence["count"] == 7
    assert recurrence["level"] == "strong"  # Постоянное решение на границе окна, за пределами списка карточек.
    assert recurrence["suggested_team"] == "КЦ Ремонт"
    assert [case["id"] for case in recurrence["cases"]] == [f"SHOW-{i}" for i in range(1, 6)]
    assert recurrence["cases"][0] == {
        "id": "SHOW-1", "title": "Сбой SHOW-1", "resolved_at": iso(just_before), "team": "autoclosedmi",
        "solution": "Восстановили вручную", "state_reason": "Отрегулирован",
    }
    assert store.search(q="CURRENT")["items"][0]["recurrence"] == recurrence
    assert store.incident("CURRENT")["recurrence"] == recurrence
    assert store.incident("BOUNDARY").get("recurrence") is None


def test_recurrence_updates_when_the_unchanged_incidents_history_changes(make_store):
    current = make("CURRENT", submitDate=iso(NOW), team=COORD)
    closed = previous("PAST", NOW - timedelta(days=1), team=COORD)
    old = previous("OLD", NOW - timedelta(days=8), reason="Постоянное решение")
    store, path = make_store([current, closed, old])
    initial = store.incident("CURRENT")
    assert initial["recurrence"]["level"] == "soft"
    assert initial["recurrence"]["suggested_team"] is None

    closed.update(stateReason="Постоянное решение", assignedGroupName="КЦ Восстановление", solution="Заменили узел")
    write(path, [current, closed, old])
    assert store.poll()
    updated = store.incident("CURRENT")
    assert updated["hash"] == initial["hash"]
    assert updated["recurrence"]["level"] == "strong"
    assert updated["recurrence"]["suggested_team"] == "КЦ Восстановление"
    assert updated["recurrence"]["cases"][0]["solution"] == "Заменили узел"

    write(path, [current, old])
    assert store.poll()
    assert store.snapshot()["incidents"]["CURRENT"].get("recurrence") is None


def test_demo_recurrence_is_first_in_coordinator_group(make_store):
    store, _ = make_store(make_demo.build(NOW))
    snap = store.snapshot()
    current_id, previous_id = "INC000030000031", "INC000030000032"
    group = next(group for group in snap["groups"] if group["key"] == "coordinators")
    assert group["ids"][0] == current_id
    current = snap["incidents"][current_id]
    assert "Повторный сбой общего узла" in current["title"]
    recurrence = current["recurrence"]
    assert recurrence["count"] == 1 and recurrence["level"] == "strong"
    assert recurrence["suggested_team"] == "ОГЭ Тридевятое царство"
    [case] = recurrence["cases"]
    assert case["id"] == previous_id
    assert case["state_reason"] == "Постоянное решение"
    assert case["solution"] == "Заменили неисправный контроллер моста и восстановили обмен маршрутами."
