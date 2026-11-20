"""Прошлый снимок против нового → список событий для ленты и подсветки."""
from __future__ import annotations

from typing import Any

from .actions import fmt_msk

FIELD_EVENTS = [
    # (поле, тип события, подпись)
    ("priority", "priority_changed", "приоритет"),
    ("deadline", "deadline_changed", "срок"),
    ("team", "team_changed", "команда"),
    ("assignee", "assignee_changed", "исполнитель"),
    ("state", "status_changed", "статус"),
]

DONE_KINDS = ("resolved", "closed", "research")


def _critical_active(inc: dict | None) -> bool:
    return bool(inc) and "critical" in inc.get("groups", []) and "critical" not in inc.get("resolved_in", [])


def _in(inc: dict | None, group: str) -> bool:
    return bool(inc) and group in inc.get("groups", []) and group not in inc.get("resolved_in", [])


def _ev(type_: str, inc: dict, text: str, before: Any = None, after: Any = None, alert: str | None = None) -> dict:
    return {
        "type": type_,
        "incident_id": inc["id"],
        "title": inc.get("title"),
        "product": inc.get("product"),
        "before": before,
        "after": after,
        "alert": alert,  # critical | coordinators | critical_resolved | None
        "text": text,
    }


def diff(old: dict[str, dict], new: dict[str, dict], ids=None) -> list[dict]:
    """ids — только эти инциденты (изменённые и пропавшие); None — все."""
    events: list[dict] = []
    if ids is None:
        ids = set(new) | set(old)
    for inc_id in sorted(i for i in ids if i in new):
        inc, prev = new[inc_id], old.get(inc_id)
        if prev is None:
            events.append(_ev("new", inc, f"новый инцидент · {inc.get('priority') or ''} · {inc.get('team') or ''}"))
        else:
            if prev.get("hash") == inc.get("hash") and prev.get("groups") == inc.get("groups") and prev.get(
                "resolved_in"
            ) == inc.get("resolved_in"):
                continue
            for field, etype, label in FIELD_EVENTS:
                if prev.get(field) != inc.get(field):
                    show = fmt_msk if field == "deadline" else (lambda v: v or "—")
                    events.append(
                        _ev(etype, inc, f"{label}: {show(prev.get(field))} → {show(inc.get(field))}", prev.get(field), inc.get(field))
                    )
            # Новые записи журнала работ: комментарий / эскалация (остальное покрыто событиями полей)
            for entry in inc.get("work_log", [])[len(prev.get("work_log", [])):]:
                if entry.get("action") in ("comment", "escalate"):
                    events.append(_ev("escalated" if entry["action"] == "escalate" else "comment", inc,
                                      ("эскалирован: " if entry["action"] == "escalate" else "комментарий: ") + (entry.get("text") or "")))
            pc, nc = (prev.get("coordinator") or {}).get("name"), (inc.get("coordinator") or {}).get("name")
            if pc != nc:
                if nc:
                    events.append(_ev("taken", inc, f"взят в работу: {nc}" + (f" (был у {pc})" if pc else ""), pc, nc))
                else:
                    events.append(_ev("released", inc, f"отпущен: {pc}", pc, None))
            pk, nk = prev.get("state_kind"), inc.get("state_kind")
            if pk == "active" and nk in DONE_KINDS:
                events.append(_ev("resolved", inc, f"решён ({inc.get('state')})", pk, nk))
            elif pk in DONE_KINDS and nk == "active":
                events.append(_ev("reopened", inc, f"возвращён в работу ({inc.get('state')})", pk, nk))

        # Переходы по группам (для новых prev=None — значит «вошёл»)
        if _critical_active(inc) and not _critical_active(prev):
            if prev is None:
                text = "новый критический"
            elif prev.get("priority") != inc.get("priority"):
                text = f"стал критическим: {prev.get('priority')} → {inc.get('priority')}"
            else:
                text = f"критический снова в работе ({inc.get('state')})"
            events.append(_ev("became_critical", inc, text, alert="critical"))
        if _critical_active(prev) and not _critical_active(inc) and inc.get("state_kind") in DONE_KINDS:
            events.append(_ev("critical_resolved", inc, f"критический решён ({inc.get('state')})", alert="critical_resolved"))
        if _in(inc, "coordinators") and not _in(prev, "coordinators"):
            events.append(_ev("to_coordinators", inc, "назначен на координаторов", alert="coordinators"))
        if _in(prev, "coordinators") and not _in(inc, "coordinators"):
            events.append(_ev("from_coordinators", inc, f"ушёл от координаторов → {inc.get('team') or '—'}"))

    # Подписываем события автором правки (последняя новая запись журнала работ)
    for ev in events:
        inc, prev = new.get(ev["incident_id"]), old.get(ev["incident_id"])
        if inc and prev and len(inc.get("work_log", [])) > len(prev.get("work_log", [])):
            ev["by"] = inc["work_log"][-1].get("author")

    for inc_id in sorted(i for i in ids if i in old and i not in new):
        events.append(_ev("removed", old[inc_id], "пропал из файла"))
    return events
