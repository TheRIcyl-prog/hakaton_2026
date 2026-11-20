"""ИИ на правилах — всегда включён, мгновенный, без сети. Числа считает только он (GigaChat пишет слова).

- выжимка: краткое описание из файла, ≤ 90 знаков (п. 8.1);
- кому передать: кандидаты по истории похожих из справочника, уверенность «8 из 10 похожих» (п. 8.2);
- риск стать критическим: балл 0–100 с разбивкой по факторам, «высокий» от 70 (п. 8.3).
Всё через справочники `index.build` за один проход — никаких сравнений «каждый со всеми».
"""
from __future__ import annotations

import re
from datetime import datetime

from ..adapter import parse_dt
from .index import Index
from .text import SUMMARY_MAX, clip, first_sentence

MASS_WORDS = re.compile(r"массов|все клиенты|у всех|недоступ|не работает|полност|регион", re.IGNORECASE)
HIGH_RISK = 70
MEDIUM_RISK = 40
CANDIDATES = 5
BACKUPS = 2
SIMILAR_SHOWN = 5


SHORT_ENOUGH = 30   # «Массово.», «Клиенты пишут.» — это не выжимка, берём первое предложение описания


def summary(inc: dict) -> str | None:
    short = inc.get("short_description")
    if short and len(short) >= SHORT_ENOUGH:
        return clip(short, SUMMARY_MAX)
    desc = first_sentence(inc.get("description"))
    best = max(filter(None, [short, desc]), key=len, default=None)
    if not best or len(best) < SHORT_ENOUGH:
        title = inc.get("title")
        best = f"{title}: {best}" if title and best and title.casefold() not in best.casefold() else best or title
    return clip(best, SUMMARY_MAX)


def similar_brief(ids: list[str], incidents: dict[str, dict]) -> list[dict]:
    out = []
    for i in ids[:SIMILAR_SHOWN]:
        o = incidents.get(i)
        if o:
            out.append({"id": i, "title": o.get("title"), "team": o.get("team"), "state": o.get("state"),
                        "url": o.get("itsm_url")})
    return out


def route(inc: dict, index: Index, incidents: dict[str, dict]) -> dict | None:
    found = index.route_stats(inc)
    if not found:
        return None
    _, basis, stats = found
    total = sum(s.count for s in stats.values())
    ranked = sorted(stats.items(), key=lambda kv: (-kv[1].count, -kv[1].solved, kv[0]))[:CANDIDATES]
    cands = [{"team": t, "count": s.count, "solved": s.solved, "score": round(s.count / total, 2),
              "similar_ids": s.examples} for t, s in ranked]
    return route_from(cands[0]["team"], cands, total, basis, incidents)


def route_from(team: str, cands: list[dict], total: int, basis: str, incidents: dict[str, dict]) -> dict:
    """Маршрут на выбранную команду; уверенность — всегда по истории этой команды."""
    best = next(c for c in cands if c["team"] == team)
    others = [c for c in cands if c["team"] != team]
    n, k = best["count"], best["solved"]
    return {
        "team": team,
        "confidence": best["score"],
        "confidence_text": f"{n} из {total} похожих",
        "basis": basis,
        "reason": f"{n} из {total} похожих {basis} вела эта команда"
                  + (f", решила {k}" if k else ""),
        "alternatives": [{"team": c["team"], "score": c["score"], "confidence_text": f"{c['count']} из {total} похожих"}
                         for c in others[:BACKUPS]],
        "candidates": [{k2: v for k2, v in c.items() if k2 != "similar_ids"} for c in cands],
        "similar_ids": best["similar_ids"],
        "similar": similar_brief(best["similar_ids"], incidents),
        "_cands": cands,          # служебное: для GigaChat, из снимка убирается
        "_total": total,
    }


def risk(inc: dict, index: Index, now: datetime, dynamics: dict | None, dup_size: int = 0) -> dict | None:
    """Для активных высоких; средние и низкие — только если ИИ заметил тревожную динамику (п. 8.3, 8.5)."""
    if inc["state_kind"] != "active" or inc["priority_level"] == "critical":
        return None
    if inc["priority_level"] != "high" and not dynamics:
        return None
    parts: list[tuple[str, int]] = []
    base = {"high": 30, "medium": 10, "low": 0}.get(inc["priority_level"], 5)
    parts.append((f"приоритет «{inc.get('priority') or '—'}»", base))

    impact, urgency = inc.get("impact_id") or 0, inc.get("urgency_id") or 0
    if impact >= 4:
        parts.append(("влияние наивысшее", 15))
    elif impact == 3:
        parts.append(("влияние высокое", 8))
    if urgency >= 4:
        parts.append(("срочность наивысшая", 10))
    elif urgency == 3:
        parts.append(("срочность высокая", 5))
    if inc.get("service_type") == "Клиентский":
        parts.append(("затронуты клиенты", 10))

    created, deadline = index.created.get(inc["id"]), parse_dt(inc.get("deadline"))
    if created and deadline and deadline > created:
        frac = (now - created) / (deadline - created)
        if frac >= 1:
            parts.append(("срок нарушен", 20))
        elif frac >= 0.75:
            parts.append((f"прошло {int(frac * 100)}% срока", 12))
        elif frac >= 0.5:
            parts.append((f"прошло {int(frac * 100)}% срока", 5))
    fc = inc.get("forecast") or {}
    if fc.get("level") == "breach":
        parts.append(("прогноз: не успеют к сроку" + (f" (обычно {fc['median_minutes']} мин)" if fc.get("median_minutes") else ""), 10))

    if MASS_WORDS.search(" ".join(filter(None, [inc.get("title"), inc.get("description")]))):
        parts.append(("признаки массовости в описании", 8))
    spike = index.spike_count(inc)
    if spike >= 3 and dup_size < 2:
        parts.append((f"ещё {spike} открытых по «{inc.get('service') or inc.get('product')}» за ±2 ч", min(12, 3 + spike)))
    if dup_size >= 2 or inc.get("is_master"):
        parts.append((f"группа похожих: {dup_size}" if dup_size >= 2 else "мастер-инцидент с дублями", 10))
    if dynamics:
        parts.append((f"нарастает: {dynamics['text']}", dynamics["boost"]))

    score = max(0, min(100, sum(p for _, p in parts)))
    level = "high" if score >= HIGH_RISK else "medium" if score >= MEDIUM_RISK else "low"
    return {
        "score": score,
        "level": level,
        "factors": [label for label, pts in parts[1:] if pts > 0],
        "breakdown": [{"label": label, "points": pts} for label, pts in parts],
        "comment": None,
    }
