from datetime import timedelta

import pytest

from backend.app.adapter import adapt_all
from backend.app.groups import assign_groups

from .conftest import COORD, NOW, make


def groups_of(rules, records, now=NOW, seen=None):
    incs, _ = adapt_all(records, rules, "{id}")
    out = assign_groups(incs, rules, now, seen)
    return incs, out


@pytest.mark.parametrize(
    "state_id,priority_id,team,expected",
    [
        (0, 4, "X", ["critical"]),
        (2, 4, "X", ["critical"]),
        (0, 3, "X", ["high"]),
        (2, 3, "X", ["high"]),
        (0, 2, "X", []),
        (0, 1, COORD, ["coordinators"]),
        (2, 2, COORD, ["coordinators"]),
        (20, 2, COORD, ["coordinators", "research"]),
        (20, 2, "X", ["research"]),
        (0, 4, COORD, ["critical", "coordinators"]),
        (2, 3, COORD, ["coordinators", "high"]),
        (7, 3, "X", []),  # решённый высокий — не в группах
        (10, 2, COORD, []),  # закрытый у координаторов — не в группах
        (7, 2, COORD, []),
    ],
)
def test_group_rules(rules, state_id, priority_id, team, expected):
    incs, _ = groups_of(rules, [make("A", state_id, priority_id, team)])
    assert incs["A"]["groups"] == expected


def test_resolved_critical_stays_30_min(rules):
    rec = make("A", 7, 4, completionDate="2026-11-10T11:45:00Z")
    incs, out = groups_of(rules, [rec])
    assert incs["A"]["groups"] == ["critical"] and incs["A"]["resolved_in"] == ["critical"]
    incs, out = groups_of(rules, [rec], now=NOW + timedelta(minutes=20))
    assert out["critical"] == []


def test_resolved_critical_without_completion_uses_seen_time(rules):
    rec = make("A", 10, 4)
    incs, _ = groups_of(rules, [rec])
    assert incs["A"]["groups"] == []  # не знаем, когда решён
    incs, _ = groups_of(rules, [rec], seen={"A": NOW - timedelta(seconds=9)})
    assert incs["A"]["resolved_in"] == ["critical"]
    _, expired = groups_of(rules, [rec], seen={"A": NOW - timedelta(seconds=10)})
    assert expired["critical"] == []


def test_critical_in_research_counts_as_resolved_in_critical(rules):
    rec = make("A", 20, 4, completionDate="2026-11-10T11:50:00Z")
    incs, _ = groups_of(rules, [rec])
    assert incs["A"]["groups"] == ["critical", "research"]
    assert incs["A"]["resolved_in"] == ["critical"]


def test_sorting_overdue_first_no_deadline_last_resolved_bottom(rules):
    records = [
        make("LATE", 0, 4, deadline="2026-11-10T11:00:00Z"),
        make("SOON", 0, 4, deadline="2026-11-10T12:30:00Z"),
        make("NONE", 0, 4, deadline=None),
        make("DONE", 7, 4, completionDate="2026-11-10T11:59:00Z", deadline="2026-11-10T11:00:00Z"),
    ]
    _, out = groups_of(rules, records)
    assert out["critical"] == ["LATE", "SOON", "NONE", "DONE"]
