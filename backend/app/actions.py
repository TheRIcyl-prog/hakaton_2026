"""Очередь «Что делать сейчас»: готовые действия для координатора с черновиками текста.

route    — инцидент на координаторах → передать в команду (подсказка ИИ + текст передачи)
escalate — критический/высокий, срок горит или прогноз «не успеют» → эскалировать (текст эскалации)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .adapter import parse_dt

MSK = timezone(timedelta(hours=3), "МСК")


def fmt_msk(value: str | None) -> str:
    dt = parse_dt(value)
    return dt.astimezone(MSK).strftime("%d.%m %H:%M МСК") if dt else "не задан"


def _left(inc: dict, now: datetime) -> str:
    dl = parse_dt(inc.get("deadline"))
    if not dl:
        return "срок не задан"
    sec = (dl - now).total_seconds()
    h, m = divmod(int(abs(sec)) // 60, 60)
    if h >= 24:
        span = f"{h // 24} д {h % 24} ч"
    else:
        span = f"{h} ч {m:02d} мин" if h else f"{m} мин"
    return f"осталось {span}" if sec >= 0 else f"просрочен на {span}"


def handover_text(inc: dict, team: str, reason: str, now: datetime) -> str:
    ai = inc.get("ai") or {}
    lines = [
        f"Коллеги, передаём вам массовый инцидент {inc['id']} «{inc['title']}».",
        "",
        f"Продукт: {inc.get('product') or '—'}; сервис: {inc.get('service') or '—'}.",
        f"Что происходит: {ai.get('summary') or inc.get('short_description') or inc.get('description') or '—'}",
        f"Приоритет: {inc.get('priority') or '—'}; влияние: {inc.get('impact') or '—'}; тип: {inc.get('service_type') or '—'}.",
        f"Срок решения: {fmt_msk(inc.get('deadline'))} ({_left(inc, now)}).",
        f"Почему вам: {reason}.",
        "",
        f"Карточка в ITSM: {inc.get('itsm_url') or inc['id']}",
        "Прошу подтвердить приём. — Координатор МИ",
    ]
    return "\n".join(lines)


def escalation_text(inc: dict, why: str, now: datetime) -> str:
    fc = inc.get("forecast") or {}
    lines = [
        f"ЭСКАЛАЦИЯ: {inc['id']} «{inc['title']}» — {inc.get('priority') or ''} приоритет.",
        "",
        f"Продукт: {inc.get('product') or '—'}. Команда: {inc.get('team') or '—'}, исполнитель: {inc.get('assignee') or 'не назначен'}.",
        f"Срок: {fmt_msk(inc.get('deadline'))} — {_left(inc, now)}.",
        f"Причина эскалации: {why}.",
    ]
    if fc.get("median_minutes"):
        lines.append(f"Типичное время решения похожих: ~{fc['median_minutes']} мин ({fc.get('basis')}).")
    lines += [
        "Прошу подключить старшего инженера / руководителя группы и дать ETA в течение 15 минут.",
        "",
        f"Карточка в ITSM: {inc.get('itsm_url') or inc['id']}",
        "— Координатор МИ",
    ]
    return "\n".join(lines)


def forecast_warning(forecast: dict | None) -> str | None:
    if not forecast:
        return None
    if forecast["level"] == "overdue":
        return "срок решения нарушен"
    if forecast["level"] == "breach":
        return f"прогноз: не успеют (типично ~{forecast['median_minutes']} мин)"
    if forecast["level"] == "risk":
        return f"прошло {int(forecast['progress'] * 100)}% срока"
    return None


def build_actions(incidents: dict[str, dict], now: datetime) -> list[dict]:
    actions: list[dict] = []
    for inc in incidents.values():
        is_crit = inc["priority_level"] == "critical" and inc["state_kind"] == "active"
        dl = parse_dt(inc.get("deadline"))
        rem = (dl - now).total_seconds() if dl else None
        urgency_time = rem if rem is not None else 10**9

        # 1. Передать от координаторов
        if inc.get("is_coordinators") and inc["state_kind"] in ("active", "research"):
            route = (inc.get("ai") or {}).get("route")
            if route:
                title = f"Передать в «{route['team']}»"
                reason = route["reason"]
            else:
                title = "Определить команду и передать"
                reason = "выберите команду вручную" if not inc.get("ai") else "похожих случаев в выгрузке нет — выберите команду вручную"
            actions.append({
                "id": f"route:{inc['id']}",
                "kind": "route",
                "incident_id": inc["id"],
                "title": title,
                "reason": reason,
                "target_team": route["team"] if route else None,
                "confidence": route["confidence"] if route else None,
                "draft": handover_text(inc, route["team"] if route else "<команда>", reason, now),
                "_score": (0 if is_crit else 1, 0, urgency_time),
            })

        # 2. Эскалировать горящий срок
        if inc["state_kind"] == "active" and inc["priority_level"] in ("critical", "high"):
            why = forecast_warning(inc.get("forecast"))
            if why:
                actions.append({
                    "id": f"escalate:{inc['id']}",
                    "kind": "escalate",
                    "incident_id": inc["id"],
                    "title": f"Эскалировать: {inc.get('team') or 'команда'}",
                    "reason": why,
                    "target_team": inc.get("team"),
                    "confidence": None,
                    "draft": escalation_text(inc, why, now),
                    "_score": (0 if is_crit else 1, 1, urgency_time),
                })

    actions.sort(key=lambda a: a["_score"])
    for a in actions:
        a.pop("_score")
    return actions
