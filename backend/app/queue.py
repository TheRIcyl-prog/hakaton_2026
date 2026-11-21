"""Персональная очередь из текущего снимка инцидентов."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from .actions import forecast_warning
from .adapter import escalation_scope_changed, norm_team, parse_dt
from .diff import DONE_KINDS
from .groups import finished_until, sort_key
from .history import History
from .outages import OutageCandidate

DEADLINE_REMINDER = timedelta(minutes=15)
COMPACT_FIELDS = ("id", "title", "product", "deadline", "priority_level", "itsm_url")
ORDER = {"claim": 0, "route": 1, "outage": 2, "escalate": 3,
         "dynamics": 4, "aftermath": 5, "deadline": 6, "recurrence": 7}


def _owner(inc: dict) -> str:
    return ((inc.get("coordinator") or {}).get("name") or "").casefold()


def _item(action: dict, inc: dict | None = None, outage_id: str | None = None) -> dict:
    return {**action, "outage_id": outage_id,
            "incident": {key: inc[key] for key in COMPACT_FIELDS} if inc else None}


def _task(kind: str, title: str, reason: str, *, inc: dict | None = None, outage_id: str | None = None) -> dict:
    inc_id = inc["id"] if inc else None
    return _item({"id": f"{kind}:{inc_id or outage_id}", "kind": kind, "incident_id": inc_id,
                  "title": title, "reason": reason, "target_team": None, "confidence": None, "draft": None},
                 inc, outage_id)


def _escalation_is_current(inc: dict, history: History) -> bool:
    entry = inc.get("_escalation")
    if not entry:
        return False
    scope = {"team": norm_team(inc["team"]), "deadline": inc["deadline"], "priority_id": inc["priority_id"]}
    if entry["scope"] is not None and entry["scope"] != scope:
        return False
    return not any(escalation_scope_changed(change) and at > entry["at"]
                   for change in history.get(inc["id"]) if (at := parse_dt(change.get("at"))))


def _deadline_notice(inc: dict, now: datetime, escalation_is_relevant: bool) -> str | None:
    deadline = parse_dt(inc["deadline"])
    if deadline is not None:
        if deadline <= now:
            return "срок решения нарушен"
        if _owner(inc) and deadline - now <= DEADLINE_REMINDER:
            return "до срока решения осталось не более 15 минут"
    return None if escalation_is_relevant else forecast_warning(inc.get("forecast"))


def _order(item: dict, incidents: dict[str, dict], now: datetime) -> tuple:
    kind, inc_id = item["kind"], item["incident_id"]
    rank = ORDER[kind]
    if inc_id is None:
        return rank, item["id"]
    group = "coordinators" if kind == "route" else "critical"
    return (rank, *sort_key(incidents[inc_id], group, now), item["id"])


def build_queue(incidents: dict[str, dict], actions: list[dict], candidates: Iterable[OutageCandidate],
                history: History, by: str, now: datetime, seen_resolved: dict[str, datetime]) -> list[dict]:
    visible = {inc_id: inc for inc_id, inc in incidents.items() if _owner(inc) in ("", by)}
    items = []
    escalations = set()
    escalations_in_queue = set()
    for action in actions:
        inc_id = action["incident_id"]
        inc = visible.get(inc_id)
        if inc is None:
            continue
        if action["kind"] == "escalate":
            escalations.add(inc_id)
            if _escalation_is_current(inc, history):
                continue
            escalations_in_queue.add(inc_id)
        items.append(_item(action, inc))

    for inc_id, inc in visible.items():
        if inc["state_kind"] in DONE_KINDS and inc["is_need_up"]:
            until = finished_until(inc, seen_resolved.get(inc_id))
            if inc["state_kind"] == "research" or (until is not None and now < until):
                items.append(_task("aftermath", "Проверить последствия", "после решения остались последствия", inc=inc))
        if inc["state_kind"] != "active":
            continue
        if inc["priority_level"] == "critical" and not _owner(inc):
            items.append(_task("claim", "Взять в работу", "критический инцидент без координатора", inc=inc))
        if inc_id not in escalations_in_queue and (reason := _deadline_notice(inc, now, inc_id in escalations)):
            items.append(_task("deadline", "Проверить срок решения", reason, inc=inc))
        dynamics = (inc.get("ai") or {}).get("dynamics") or {}
        if dynamics.get("flag"):
            items.append(_task("dynamics", "Посмотреть тревожную динамику", dynamics.get("text") or "нарастает", inc=inc))
        if recurrence := inc.get("recurrence"):
            items.append(_task("recurrence", "Посмотреть прошлые случаи",
                               f"прошлых случаев на этом узле: {recurrence['count']}", inc=inc))

    for candidate in candidates:
        summary = candidate.summary
        if all(inc_id in visible for inc_id in summary["incident_ids"]):
            items.append(_task("outage", summary["name"], summary["reason"], outage_id=summary["id"]))
    return sorted(items, key=lambda item: _order(item, incidents, now))
