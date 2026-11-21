"""История повторов по узлу (сервис и сценарий)."""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta

from .adapter import norm_team, parse_dt
from .diff import DONE_KINDS

WINDOW = timedelta(days=7)
CASES_LIMIT = 5
CASE_FIELDS = ("id", "title", "resolved_at", "team", "solution", "state_reason")


class _NodeHistory:
    def __init__(self, records: list[tuple[datetime, dict]], excluded_teams: set[str]):
        records.sort(key=lambda record: (record[0], record[1]["id"]))
        self.dates: list[datetime] = []
        self.cases: list[dict] = []
        self.last_permanent: list[int] = []
        self.last_eligible_team: list[int] = []
        permanent = eligible_team = -1
        for i, (resolved, inc) in enumerate(records):
            self.dates.append(resolved)
            self.cases.append({key: inc[key] for key in CASE_FIELDS})
            if inc["state_reason"] == "Постоянное решение":
                permanent = i
            if norm_team(inc["team"]) not in excluded_teams:
                eligible_team = i
            self.last_permanent.append(permanent)
            self.last_eligible_team.append(eligible_team)

    def recurrence(self, created: datetime) -> dict | None:
        first = bisect_left(self.dates, created - WINDOW)
        end = bisect_left(self.dates, created)
        if first == end:
            return None
        eligible_team = self.last_eligible_team[end - 1]
        return {
            "count": end - first,
            "level": "strong" if self.last_permanent[end - 1] >= first else "soft",
            "suggested_team": self.cases[eligible_team]["team"] if eligible_team >= first else None,
            "cases": list(reversed(self.cases[max(first, end - CASES_LIMIT):end])),
        }


def apply_recurrences(incidents: dict[str, dict], coordinators_team: str) -> None:
    """Строит индекс один раз на обновление файла. Карточка читает только своё окно и до пяти случаев."""
    active = defaultdict(list)
    history = defaultdict(list)
    for inc in incidents.values():
        inc.pop("recurrence", None)
        service, scenario = inc["service"], inc["scenario"]
        if not (service and scenario):
            continue
        node = (service, scenario)
        if inc["state_kind"] == "active":
            if created := parse_dt(inc["created_at"]):
                active[node].append((created, inc))
        elif inc["state_kind"] in DONE_KINDS:
            if resolved := parse_dt(inc["resolved_at"]):
                history[node].append((resolved, inc))

    excluded_teams = {"", "autoclosedmi", norm_team(coordinators_team)}
    for node in active.keys() & history.keys():
        node_history = _NodeHistory(history[node], excluded_teams)
        for created, inc in active[node]:
            if recurrence := node_history.recurrence(created):
                inc["recurrence"] = recurrence
