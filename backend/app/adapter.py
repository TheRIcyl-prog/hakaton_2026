"""Единственное место, которое знает формат выгрузки МТС.

Сырые записи → нормализованный Incident (dict). Ничего не дописываем в файл,
кривые поля не роняют разбор: в худшем случае поле станет None.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from datetime import datetime, timezone
from typing import Any

from .config import Rules

ZERO_DATE_PREFIX = "0001-01-01"
# Структурные поля записи журнала (было → стало), формат history.py
WORK_LOG_EXTRA = ("kind", "before", "after", "before_id", "after_id", "up", "later", "shift_min")


def parse_dt(value: Any) -> datetime | None:
    """ISO с Z / микросекундами / без зоны, а также 'YYYY-MM-DD HH:MM:SS'. Всё приводим к UTC."""
    if not value or not isinstance(value, str):
        return None
    return _parse_dt_str(value)


@lru_cache(maxsize=200_000)  # одни и те же строки дат разбираются на каждой правке — кэшируем (datetime неизменяем)
def _parse_dt_str(value: str) -> datetime | None:
    s = value.strip()
    if not s or s.startswith(ZERO_DATE_PREFIX):
        return None
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    s = s.replace(" ", "T", 1)
    # Python 3.11+ понимает до 6 знаков дробной части; лишние отрежем
    if "." in s:
        head, _, tail = s.partition(".")
        digits = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                digits += ch
            else:
                rest = tail[i:]
                break
        s = f"{head}.{digits[:6]}{rest}" if digits else head + rest
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # уточнить у менторов; в выгрузке всё в Z
    return dt.astimezone(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("true", "1", "да", "yes"):
        return True
    if s in ("false", "0", "нет", "no"):
        return False
    return None


def to_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def norm_team(name: str | None) -> str:
    return " ".join((name or "").split()).casefold()


def resolve_state_id(raw: dict, rules: Rules) -> int | None:
    sid = to_int(raw.get("stateId"))
    if sid is not None and sid in rules.states:
        return sid
    enum = to_str(raw.get("stateEnum"))
    if enum and enum in rules.state_enum:
        return rules.state_enum[enum]
    text = to_str(raw.get("state"))
    if text:
        for k, v in rules.states.items():
            if v["name"].casefold() == text.casefold():
                return k
    return sid


def resolve_priority_id(raw: dict, rules: Rules) -> int | None:
    pid = to_int(raw.get("priorityId"))
    if pid is not None and pid in rules.priorities:
        return pid
    text = to_str(raw.get("priority"))
    if text and text in rules.priority_text:
        return rules.priority_text[text]
    return pid


def content_hash(raw: dict) -> str:
    return hashlib.sha1(json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:12]


def _work_log(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for e in value[-30:]:
        if isinstance(e, dict):
            entry = {
                "at": iso(parse_dt(e.get("at"))),
                "author": to_str(e.get("author")),
                "action": to_str(e.get("action")),
                "text": to_str(e.get("text")),
            }
            # Структурная часть (было → стало) — её пишет itsm.py; нужна истории изменений (ИИ, п. 8.5)
            if e.get("kind"):
                for k in WORK_LOG_EXTRA:
                    if e.get(k) is not None:
                        entry[k] = e.get(k)
            out.append(entry)
    return out


def escalation_scope_changed(change: dict) -> bool:
    kind = change.get("kind")
    before, after = change.get("before"), change.get("after")
    if kind == "team":
        return norm_team(to_str(before)) != norm_team(to_str(after))
    if kind == "deadline":
        return parse_dt(before) != parse_dt(after)
    if kind == "priority":
        return change.get("before_id", before) != change.get("after_id", after)
    return kind == "reopen"


def _latest_escalation(value: Any) -> dict | None:
    if not isinstance(value, list):
        return None
    for entry in reversed(value):
        if not isinstance(entry, dict):
            continue
        if entry.get("action") == "escalate":
            if at := parse_dt(entry.get("at")):
                return {"at": at, "scope": entry.get("escalation_scope")}
        if escalation_scope_changed(entry):
            break
    return None


def _coordinator(value: Any) -> dict | None:
    """Наше поле «координатор»: кто из координаторов взял инцидент в работу (decisions.md, п. 7)."""
    if not isinstance(value, dict):
        return None
    name = to_str(value.get("name"))
    if not name:
        return None
    return {"name": name, "at": iso(parse_dt(value.get("at")))}


def outage_excluded_with(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {other for scope in value if isinstance(scope, dict) and isinstance(scope.get("incident_ids"), list)
            for other in scope["incident_ids"] if isinstance(other, str)}


def outage_link(value: Any) -> dict | None:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"].strip():
        return None
    return {key: to_str(value.get(key)) for key in ("id", "master_id", "name", "reason", "source")}


def adapt_one(raw: dict, rules: Rules, itsm_template: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    inc_id = to_str(raw.get("id"))
    if not inc_id:
        return None
    state_id = resolve_state_id(raw, rules)
    priority_id = resolve_priority_id(raw, rules)
    state_kind = rules.state_kind(state_id)
    team = to_str(raw.get("assignedGroupName"))
    sys_id = to_str(raw.get("sysId")) or ""
    try:
        itsm_url = itsm_template.format(id=inc_id, sysId=sys_id)
    except (KeyError, IndexError, ValueError):
        itsm_url = None

    incident = {
        "id": inc_id,
        "sys_id": sys_id or None,
        "title": to_str(raw.get("header")) or inc_id,
        "short_description": to_str(raw.get("shortDescription")),
        "description": to_str(raw.get("description")),
        # solution встречается и у активных — показываем только у решённых
        "solution": to_str(raw.get("solution")) if state_kind in ("resolved", "closed", "research") else None,
        "product": to_str(raw.get("productName")),
        "product_code": to_str(raw.get("productMasterId")),
        "service": to_str(raw.get("service")),
        "scenario": to_str(raw.get("scenarioName")),
        "state_id": state_id,
        "state": rules.states.get(state_id, {}).get("name") if state_id in rules.states else to_str(raw.get("state")),
        "state_kind": state_kind,  # active | resolved | closed | research | unknown
        "state_reason": to_str(raw.get("stateReason")),
        "priority_id": priority_id,
        "priority": rules.priorities.get(priority_id, {}).get("name")
        if priority_id in rules.priorities
        else to_str(raw.get("priority")),
        "priority_level": rules.priority_level(priority_id),  # critical | high | medium | low | unknown
        "impact_id": to_int(raw.get("impactId")),
        "impact": to_str(raw.get("impact")),
        "urgency_id": to_int(raw.get("urgencyId")),
        "urgency": to_str(raw.get("urgency")),
        "service_type": to_str(raw.get("serviceType")),
        "team": team,
        "is_coordinators": norm_team(team) == norm_team(rules.coordinators_team),
        "assignee": to_str(raw.get("assignedEmployee")),
        "assignee_login": to_str(raw.get("assignedEmployeeLogin")),
        "initiator": to_str(raw.get("initiatorName")),
        "channel": to_str(raw.get("receivingChannel")),
        "created_at": iso(parse_dt(raw.get("submitDate"))),
        "deadline": iso(parse_dt(raw.get("deadline"))),
        "resolved_at": iso(parse_dt(raw.get("completionDate"))),
        "assigned_to_team_at": iso(parse_dt(raw.get("lastAssignmentToTeamDateTime"))),
        "is_active": state_kind == "active",
        "is_master": bool(to_bool(raw.get("isMaster"))),
        "is_double": bool(to_bool(raw.get("isDouble"))),
        "is_reason": bool(to_bool(raw.get("isReason"))),
        "is_effect": bool(to_bool(raw.get("isEffect"))),
        "is_need_up": to_bool(raw.get("isNeedUp")),
        "itsm_url": itsm_url,
        # Журнал работ — наше поле (менторы разрешили добавлять поля). Пишет его itsm.py.
        "work_log": _work_log(raw.get("workLog")),
        # «Взять в работу» — наше поле coordinator: {name, at}. Пишет его itsm.py.
        "coordinator": _coordinator(raw.get("coordinator")),
        "hash": content_hash(raw),
    }
    cause = raw.get("causeIncidentId")
    if isinstance(cause, str) and (cause_id := cause.strip()):
        incident["cause_incident_id"] = cause_id
    link = outage_link(raw.get("outage"))
    if link is not None:
        incident["outage"] = link
    if excluded_with := outage_excluded_with(raw.get("outageExclusions")):
        incident["_outage_excluded_with"] = excluded_with
    if escalation := _latest_escalation(raw.get("workLog")):
        incident["_escalation"] = escalation
    return incident


def unwrap(data: Any) -> list:
    """Массив инцидентов (на случай обёртки {"items": [...]} / {"incidents": [...]})."""
    if isinstance(data, dict):
        for key in ("incidents", "items", "data", "result"):
            if isinstance(data.get(key), list):
                return data[key]
    if not isinstance(data, list):
        raise ValueError("ожидается JSON-массив инцидентов")
    return data


def adapt_all(data: Any, rules: Rules, itsm_template: str,
              prev: dict[str, tuple[dict, dict]] | None = None) -> tuple[dict[str, dict], list[str]]:
    """Возвращает {id: incident} и список предупреждений. Повтор id — побеждает последний.

    prev = {id: (сырая запись, incident)} с прошлого чтения: если запись не изменилась (сравнение dict в C —
    0,01 с на 10 000), берём готовый incident без разбора и без пересчёта hash."""
    warnings: list[str] = []
    data = unwrap(data)
    prev = prev or {}
    skipped = 0
    out: dict[str, dict] = {}
    for i, raw in enumerate(data):
        old = prev.get(raw.get("id")) if isinstance(raw, dict) and isinstance(raw.get("id"), str) else None
        if old is not None and old[0] == raw:
            inc = old[1]
        else:
            inc = adapt_one(raw, rules, itsm_template)
        if inc is None:
            skipped += 1
            if skipped <= 10:
                warnings.append(f"запись #{i}: нет id или не объект — пропущена")
            continue
        if inc["id"] in out:
            warnings.append(f"повтор id {inc['id']} — взята последняя запись")
        out[inc["id"]] = inc
    if skipped:
        warnings.insert(0, f"пропущено записей без id: {skipped}")
    return out, warnings


def export_moment(data: Any) -> datetime | None:
    """Момент выгрузки ≈ самая поздняя дата регистрации/закрытия/назначения в файле."""
    best: datetime | None = None
    if not isinstance(data, list):
        return None
    for raw in data:
        if not isinstance(raw, dict):
            continue
        for key in ("submitDate", "completionDate", "lastAssignmentToTeamDateTime"):
            dt = parse_dt(raw.get(key))
            if dt and (best is None or dt > best):
                best = dt
    return best
