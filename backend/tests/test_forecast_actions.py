from backend.app.actions import build_actions
from backend.app.adapter import adapt_all
from backend.app.ai import enrich
from backend.app.forecast import forecast_all

from .conftest import COORD, NOW, make


def prepare(rules, records):
    incs, _ = adapt_all(records, rules, "https://itsm/{id}")
    forecast_all(incs, NOW)
    enrich(incs, NOW, rules)
    return incs


def history(team="КЦ ДОИ VMWare", minutes=60, n=3, prio=4):
    # решённые за `minutes` минут
    return [
        make(f"H{i}", 7, prio, team, submitDate="2026-11-09T10:00:00Z",
             completionDate=f"2026-11-09T{10 + minutes // 60:02d}:{minutes % 60:02d}:00Z")
        for i in range(n)
    ]


def test_forecast_breach_when_typical_longer_than_deadline(rules):
    # срок 1 ч от создания, а такие решаются 3 ч
    rec = make("A", 2, 4, "КЦ ДОИ VMWare", submitDate="2026-11-10T11:40:00Z", deadline="2026-11-10T12:40:00Z")
    incs = prepare(rules, [rec, *history(minutes=180)])
    fc = incs["A"]["forecast"]
    assert fc["level"] == "breach" and fc["median_minutes"] == 180 and "команда и приоритет" in fc["basis"]


def test_forecast_ok_and_overdue(rules):
    ok = make("OK", 2, 4, "КЦ ДОИ VMWare", submitDate="2026-11-10T11:50:00Z", deadline="2026-11-10T20:00:00Z")
    late = make("LATE", 2, 4, "КЦ ДОИ VMWare", submitDate="2026-11-10T08:00:00Z", deadline="2026-11-10T11:00:00Z")
    incs = prepare(rules, [ok, late, *history(minutes=30)])
    assert incs["OK"]["forecast"]["level"] == "ok"
    assert incs["LATE"]["forecast"]["level"] == "overdue"
    assert incs["H0"]["forecast"] is None  # у решённых прогноза нет


def test_actions_route_and_escalate_ordered(rules):
    coord = make("C", 0, 2, COORD, service="[Летающий ковер] Ворс")
    helper = make("R", 7, 2, "КЦ ДОИ VMWare", service="[Летающий ковер] Ворс", completionDate="2026-11-10T11:00:00Z")
    crit_late = make("K", 2, 4, "КЦ ИТ Мой МТС", submitDate="2026-11-10T08:00:00Z", deadline="2026-11-10T11:00:00Z")
    incs = prepare(rules, [coord, helper, crit_late])
    acts = build_actions(incs, NOW)
    assert [a["id"] for a in acts] == ["escalate:K", "route:C"]  # критический первым
    route = acts[1]
    assert route["target_team"] == "КЦ ДОИ VMWare"
    assert "КЦ ДОИ VMWare" not in route["draft"] or "C" in route["draft"]
    assert "https://itsm/C" in route["draft"]
    assert "просрочен" in acts[0]["draft"]


def test_route_without_similar_still_asks_to_hand_over(rules):
    incs = prepare(rules, [make("C", 20, 2, COORD)])
    acts = build_actions(incs, NOW)
    assert acts[0]["kind"] == "route" and acts[0]["target_team"] is None
