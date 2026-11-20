"""Incident → группы по правилам из config/rules.yaml."""
from __future__ import annotations

from datetime import datetime, timedelta

from .adapter import norm_team, parse_dt
from .config import GroupRule, Rules


def _match(inc: dict, rule: GroupRule) -> bool:
    if rule.priority_ids is not None and inc["priority_id"] not in rule.priority_ids:
        return False
    if rule.teams is not None and norm_team(inc["team"]) not in {norm_team(t) for t in rule.teams}:
        return False
    if rule.state_ids is not None and inc["state_id"] not in rule.state_ids:
        return False
    return True


def resolved_moment(inc: dict, seen_resolved_at: datetime | None) -> datetime | None:
    """Когда решён: completionDate из файла, иначе когда сервер увидел переход в решённый."""
    return parse_dt(inc.get("resolved_at")) or seen_resolved_at


def recently_resolved(inc: dict, rule: GroupRule, now: datetime, seen_resolved_at: datetime | None) -> bool:
    if rule.show_resolved_minutes <= 0 or inc["state_kind"] not in ("resolved", "closed", "research"):
        return False
    if rule.priority_ids is not None and inc["priority_id"] not in rule.priority_ids:
        return False
    if rule.teams is not None and norm_team(inc["team"]) not in {norm_team(t) for t in rule.teams}:
        return False
    at = resolved_moment(inc, seen_resolved_at)
    if at is None:
        return False
    # Дата решения «из будущего» (кто-то вписал руками) — тоже показываем
    return now - at <= timedelta(minutes=rule.show_resolved_minutes)


def assign_groups(
    incidents: dict[str, dict],
    rules: Rules,
    now: datetime,
    seen_resolved: dict[str, datetime] | None = None,
) -> dict[str, list[str]]:
    """Проставляет inc['groups'], inc['resolved_in'] и возвращает {group: [id, ...]} в порядке показа."""
    seen_resolved = seen_resolved or {}
    out: dict[str, list[str]] = {g.key: [] for g in rules.groups}
    for inc in incidents.values():
        inc["groups"] = []
        inc["resolved_in"] = []  # группы, где инцидент висит как «решён»
        for rule in rules.groups:
            if _match(inc, rule):
                inc["groups"].append(rule.key)
                out[rule.key].append(inc["id"])
            elif recently_resolved(inc, rule, now, seen_resolved.get(inc["id"])):
                inc["groups"].append(rule.key)
                inc["resolved_in"].append(rule.key)
                out[rule.key].append(inc["id"])
    for key, ids in out.items():
        ids.sort(key=lambda i: sort_key(incidents[i], key, now))
    return out


def remaining_seconds(inc: dict, now: datetime) -> float | None:
    dl = parse_dt(inc.get("deadline"))
    return (dl - now).total_seconds() if dl else None


def sort_key(inc: dict, group: str, now: datetime) -> tuple:
    """Решённые вниз; остальные — по оставшемуся времени (просроченные первыми), без срока — в конец."""
    is_resolved_here = group in inc.get("resolved_in", [])
    rem = remaining_seconds(inc, now)
    return (
        1 if is_resolved_here else 0,
        0 if rem is not None else 1,
        rem if rem is not None else 0,
        -(inc.get("priority_id") or 0),
        inc.get("created_at") or "",
    )
