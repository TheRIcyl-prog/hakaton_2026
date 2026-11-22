"""Incident → группы по правилам из config/rules.yaml."""
from __future__ import annotations

from collections.abc import Set
from datetime import datetime, timedelta

from .adapter import norm_team, parse_dt
from .config import GroupRule, Rules

DONE = ("resolved", "closed", "research")
FINISHED_MINUTES = 30
CLOSED_SECONDS = 10


def _teams(rule: GroupRule) -> frozenset | None:
    """Нормализованные команды правила — считаем один раз и храним на самом правиле."""
    cached = rule.__dict__.get("_norm_teams", False)
    if cached is False:
        cached = frozenset(norm_team(t) for t in rule.teams) if rule.teams is not None else None
        rule.__dict__["_norm_teams"] = cached
    return cached


def _match(inc: dict, rule: GroupRule) -> bool:
    if rule.priority_ids is not None and inc["priority_id"] not in rule.priority_ids:
        return False
    teams = _teams(rule)
    if teams is not None and norm_team(inc["team"]) not in teams:
        return False
    if rule.state_ids is not None and inc["state_id"] not in rule.state_ids:
        return False
    return True


def resolved_moment(inc: dict, seen_resolved_at: datetime | None) -> datetime | None:
    """Дата завершения из файла, для закрытых в первую очередь момент закрытия."""
    completion = parse_dt(inc.get("resolved_at"))
    return (seen_resolved_at or completion) if inc["state_kind"] == "closed" else (completion or seen_resolved_at)


def finished_until(inc: dict, seen_at: datetime | None, minutes: int = FINISHED_MINUTES) -> datetime | None:
    if inc["state_kind"] not in DONE:
        return None
    at = resolved_moment(inc, seen_at)
    if at is None:
        return None
    window = timedelta(minutes=minutes)
    if inc["state_kind"] == "closed":
        window = min(window, timedelta(seconds=CLOSED_SECONDS))
    return at + window


def recently_resolved(inc: dict, rule: GroupRule, now: datetime, seen_resolved_at: datetime | None) -> bool:
    if rule.priority_ids is not None and inc["priority_id"] not in rule.priority_ids:
        return False
    teams = _teams(rule)
    if teams is not None and norm_team(inc["team"]) not in teams:
        return False
    until = finished_until(inc, seen_resolved_at, rule.show_resolved_minutes)
    return until is not None and now < until


def assign_groups(
    incidents: dict[str, dict],
    rules: Rules,
    now: datetime,
    seen_resolved: dict[str, datetime] | None = None,
    closed_from_research: Set[str] = frozenset(),
) -> dict[str, list[str]]:
    """Проставляет inc['groups'], inc['resolved_in'] и возвращает {group: [id, ...]} в порядке показа."""
    seen_resolved = seen_resolved or {}
    out: dict[str, list[str]] = {g.key: [] for g in rules.groups}
    for inc in incidents.values():
        groups: list[str] = []
        resolved_in: list[str] = []  # группы, где инцидент висит как «решён»
        done = inc["state_kind"] in DONE
        if inc["state_kind"] == "closed":
            until = finished_until(inc, seen_resolved.get(inc["id"]))
            inc["closed_until"] = until.isoformat() if until else None
        for rule in rules.groups:
            if _match(inc, rule):
                groups.append(rule.key)
                out[rule.key].append(inc["id"])
            elif (done and rule.show_resolved_minutes > 0
                  and (rule.key != "research" or inc["id"] in closed_from_research)
                  and (inc["state_kind"] != "research" or rule.key == "critical")
                  and recently_resolved(inc, rule, now, seen_resolved.get(inc["id"]))):
                groups.append(rule.key)
                resolved_in.append(rule.key)
                out[rule.key].append(inc["id"])
        inc["groups"] = groups
        inc["resolved_in"] = resolved_in
    for key, ids in out.items():
        ids.sort(key=lambda i: sort_key(incidents[i], key, now))
    return out


def remaining_seconds(inc: dict, now: datetime) -> float | None:
    dl = parse_dt(inc.get("deadline"))
    return (dl - now).total_seconds() if dl else None


def waiting_since(inc: dict) -> datetime | None:
    """С какого момента инцидент ждёт у координаторов: последнее назначение на команду, иначе регистрация."""
    return parse_dt(inc.get("assigned_to_team_at")) or parse_dt(inc.get("created_at"))


def sort_key(inc: dict, group: str, now: datetime) -> tuple:
    """Порядок колонки (decisions.md, п. 3):
    на координаторах — дольше всех ждёт сверху; исследование — новые сверху;
    остальные — решённые вниз, просроченные первыми, дальше по оставшемуся времени, без срока — в конец."""
    if group == "coordinators":
        w = waiting_since(inc)
        return (0 if w else 1, w.timestamp() if w else 0, -(inc.get("priority_id") or 0))
    if group == "research":
        at = parse_dt(inc.get("resolved_at")) or parse_dt(inc.get("created_at"))
        return (0 if at else 1, -at.timestamp() if at else 0)
    is_resolved_here = group in inc.get("resolved_in", [])
    rem = remaining_seconds(inc, now)
    return (
        1 if is_resolved_here else 0,
        0 if rem is not None else 1,
        rem if rem is not None else 0,
        -(inc.get("priority_id") or 0),
        inc.get("created_at") or "",
    )
