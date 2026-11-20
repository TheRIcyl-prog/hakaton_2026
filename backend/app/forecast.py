"""Прогноз нарушения срока по истории: медиана времени решения (completionDate − submitDate).

Медиану берём сначала по (команда, приоритет), если примеров ≥ MIN_SAMPLES, потом по приоритету,
потом по всем решённым. Медианы считаются один раз на правку (refs.Refs), не на каждый инцидент.
LLM тут не нужен — это честная статистика по выгрузке.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .adapter import iso, parse_dt
from .refs import Refs


def forecast_all(incidents: dict[str, dict], now: datetime, refs: Refs | None = None) -> None:
    refs = refs or Refs(incidents, now)
    for inc in incidents.values():
        inc["forecast"] = forecast_one(inc, refs, now)


def forecast_one(inc: dict, refs: Refs, now: datetime) -> dict | None:
    if inc["state_kind"] != "active":
        return None
    created, deadline = parse_dt(inc.get("created_at")), parse_dt(inc.get("deadline"))
    if not created or not deadline or deadline <= created:
        return None
    total = (deadline - created).total_seconds()
    elapsed = (now - created).total_seconds()
    progress = max(0.0, elapsed / total)
    med, basis, n = refs.median_resolution(inc.get("team"), inc.get("priority_id"))
    eta = None
    if med is not None:
        eta = created + timedelta(seconds=med)
        if eta < now:
            # Уже дольше типичного: ждём ещё хотя бы половину медианы
            eta = now + timedelta(seconds=med / 2)
    if now >= deadline:
        level = "overdue"
    elif eta and eta > deadline:
        level = "breach"  # прогноз: не успеют
    elif progress >= 0.75:
        level = "risk"
    else:
        level = "ok"
    return {
        "progress": round(min(progress, 9.99), 3),  # доля истёкшего срока
        "level": level,  # ok | risk | breach | overdue
        "eta": iso(eta),
        "median_minutes": round(med / 60) if med is not None else None,
        "basis": f"медиана по {n} решённым ({basis})" if n else basis,
    }
