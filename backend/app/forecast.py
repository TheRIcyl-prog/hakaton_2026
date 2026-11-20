"""Прогноз нарушения срока по истории: медиана времени решения (completionDate − submitDate).

Медиану берём сначала по (команда, приоритет), если примеров ≥ MIN_SAMPLES, потом по приоритету,
потом по всем решённым. LLM тут не нужен — это честная статистика по выгрузке.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median

from .adapter import iso, parse_dt

MIN_SAMPLES = 2
DONE = ("resolved", "closed", "research")


def _durations(incidents: dict[str, dict]) -> list[tuple[str | None, int | None, float]]:
    out = []
    for inc in incidents.values():
        if inc["state_kind"] not in DONE:
            continue
        c, r = parse_dt(inc.get("created_at")), parse_dt(inc.get("resolved_at"))
        if c and r and r > c:
            out.append((inc.get("team"), inc.get("priority_id"), (r - c).total_seconds()))
    return out


def _pick(durs, team, prio) -> tuple[float | None, str, int]:
    by_tp = [d for t, p, d in durs if t == team and p == prio]
    if len(by_tp) >= MIN_SAMPLES:
        return median(by_tp), "команда и приоритет", len(by_tp)
    by_p = [d for t, p, d in durs if p == prio]
    if len(by_p) >= MIN_SAMPLES:
        return median(by_p), "приоритет", len(by_p)
    all_d = [d for _, _, d in durs]
    if all_d:
        return median(all_d), "все решённые", len(all_d)
    return None, "нет истории", 0


def forecast_all(incidents: dict[str, dict], now: datetime) -> None:
    durs = _durations(incidents)
    for inc in incidents.values():
        inc["forecast"] = forecast_one(inc, durs, now)


def forecast_one(inc: dict, durs, now: datetime) -> dict | None:
    created, deadline = parse_dt(inc.get("created_at")), parse_dt(inc.get("deadline"))
    if inc["state_kind"] != "active" or not created or not deadline or deadline <= created:
        return None
    total = (deadline - created).total_seconds()
    elapsed = (now - created).total_seconds()
    progress = max(0.0, elapsed / total)
    med, basis, n = _pick(durs, inc.get("team"), inc.get("priority_id"))
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
