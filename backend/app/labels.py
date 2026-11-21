"""Метки инцидентов из выгрузки, истории и совпадений по узлу."""
from datetime import datetime, timedelta

from .adapter import norm_team, parse_dt, to_str
from .history import History

REASSIGN_WINDOW = timedelta(hours=4)
REASSIGN_COUNT = 3


def apply_labels(incidents: dict[str, dict], history: History, now: datetime) -> None:
    since = now - REASSIGN_WINDOW
    for inc in incidents.values():
        labels = []
        if norm_team(inc["state_reason"]) == "внешняя обработка":
            labels.append("waiting_external")
        if inc["is_need_up"]:
            labels.append("aftermath")
        if norm_team(inc["service_type"]) == "клиентский":
            labels.append("clients_affected")
        if _recent_transfers(history.get(inc["id"]), since, now) >= REASSIGN_COUNT:
            labels.append("reassigned")
        if inc.get("recurrence"):
            labels.append("recurrence")
        if labels:
            inc["labels"] = labels
        else:
            inc.pop("labels", None)


def _recent_transfers(events: list[dict], since: datetime, now: datetime) -> int:
    count = 0
    for entry in events:
        if entry["kind"] != "team":
            continue
        at = parse_dt(entry.get("at"))
        before = norm_team(to_str(entry.get("before")))
        after = norm_team(to_str(entry.get("after")))
        if at and since <= at <= now and before and after and before != after:
            count += 1
    return count
